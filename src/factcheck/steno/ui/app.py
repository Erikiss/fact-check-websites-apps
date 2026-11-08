"""Live steno mode: capture loop, trigger logic, and the glue between engine events and the windows.

Local context: `StenoLiveApp` grabs the chosen region STENO_FPS times per second, feeds tiny signatures to
`ChangeGate`, submits frames to `LiveEngine`, and turns engine events into overlay/panel updates:
  * fast marks are drawn as they stream in; a newer picture replaces the overlay at its first mark;
  * a deep result replaces the overlay in one go once it is complete;
  * results for pictures older than the one on screen are dropped; failed calls keep the last good overlay.
On Windows our windows are excluded from screen capture (the model never sees the overlay). Elsewhere the
overlay is hidden for the moment a frame for the model is grabbed, and the gate is re-based after our repaints.
Global context: started by `factcheck-steno` (cli.py) through `run_live`.
"""

from __future__ import annotations

import itertools
import json
import logging
import sys
import time

import numpy as np
from PIL import Image
from PySide6.QtCore import QObject, QRect, Qt, QTimer, Signal
from PySide6.QtGui import QGuiApplication, QImage, QKeySequence, QShortcut
from PySide6.QtWidgets import QApplication

from factcheck.config import Settings
from factcheck.steno.engine import Frame, LiveEngine
from factcheck.steno.frames import ChangeGate, GateConfig, Trigger, changed_fraction
from factcheck.steno.models import StenoResult
from factcheck.steno.ui.overlay import OverlayWindow
from factcheck.steno.ui.widgets import ControlBar, DetailsPanel, RegionSelector
from factcheck.steno.ui.winapi import exclude_from_capture, make_global_hotkeys, qt_sequence
from factcheck.steno.vision import VisionBackend, stages_from_settings

log = logging.getLogger(__name__)
SIGNATURE_WIDTH = 160
STAGE_LABEL = {"fast": "⚡ schnell", "deep": "🔍 genau"}
MAX_FAST_FAILURES = 3            # consecutive failed fast calls before live mode pauses itself


def qimage_signature(img: QImage, width: int = SIGNATURE_WIDTH) -> np.ndarray:
    """Grayscale thumbnail for change detection (Qt does the downscaling in C++)."""
    h = max(1, round(width * img.height() / max(1, img.width())))
    small = img.scaled(width, h, Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation)
    small = small.convertToFormat(QImage.Format.Format_Grayscale8)
    raw = np.frombuffer(small.constBits(), dtype=np.uint8, count=small.bytesPerLine() * small.height())
    return raw.reshape(small.height(), small.bytesPerLine())[:, : small.width()].astype(np.float32)


def qimage_to_pil(img: QImage) -> Image.Image:
    rgb = img.convertToFormat(QImage.Format.Format_RGB888)
    w, h, bpl = rgb.width(), rgb.height(), rgb.bytesPerLine()
    raw = np.frombuffer(rgb.constBits(), dtype=np.uint8, count=bpl * h).reshape(h, bpl)
    return Image.fromarray(raw[:, : w * 3].reshape(h, w, 3).copy(), "RGB")


def parse_region(text: str | None) -> QRect | None:
    """'x,y,w,h' (global screen coordinates) -> QRect."""
    if not text:
        return None
    try:
        x, y, w, h = (int(float(v)) for v in text.split(","))
    except ValueError as exc:
        raise SystemExit(f"--region erwartet x,y,w,h, nicht {text!r}") from exc
    return QRect(x, y, w, h) if w > 0 and h > 0 else None


def headline(result: StenoResult) -> str:
    return f"{STAGE_LABEL.get(result.stage, result.stage)} · {result.served_by or result.model} · {result.duration_s:.1f}s"


class StenoLiveApp(QObject):
    engine_event = Signal(object)                 # emitted from the engine thread, delivered on the GUI thread

    def __init__(self, settings: Settings, backend: VisionBackend, *, deep: bool = True,
                 region: QRect | None = None) -> None:
        super().__init__()
        self.settings = settings
        self.backend = backend
        self.stages = stages_from_settings(settings)
        if not deep:
            self.stages.pop("deep", None)
        self.deep_auto = "deep" in self.stages and settings.steno_deep_auto
        self.gate = ChangeGate(GateConfig(
            settle_s=settings.steno_settle_s, max_wait_s=settings.steno_max_wait_s,
            min_interval_s=settings.steno_min_interval_s, change_threshold=settings.steno_change_threshold,
            deep_after_s=settings.steno_deep_after_s if self.deep_auto else 0.0))
        self.hint = settings.steno_hint
        self.engine = LiveEngine(backend, self.stages, on_event=self.engine_event.emit,
                                 max_image_edge=settings.steno_max_image_edge,
                                 max_image_tokens=settings.steno_max_image_tokens, hint_provider=lambda: self.hint)
        self.engine_event.connect(self._on_event, Qt.ConnectionType.QueuedConnection)

        self.overlay = OverlayWindow()
        self.controls = ControlBar(hint=self.hint, deep_available="deep" in self.stages)
        self.panel = DetailsPanel()
        self._wire_controls()

        self.live = True
        self.screen = None
        self.region: QRect | None = None
        self.initial_region = region
        self.excluded = False                     # True when Windows keeps our windows out of captures
        self._selectors: list[RegionSelector] = []
        self._grabbing = False
        self._frame_ids = itertools.count(1)
        self._frame_sigs: dict[int, np.ndarray] = {}
        self._image_sizes: dict[int, tuple[int, int]] = {}   # frame id -> size of the image the model saw
        self._last_sig: np.ndarray | None = None
        self._last_image: QImage | None = None
        self._shown_frame = 0                     # frame whose marks the overlay shows
        self._shown_stage = ""
        self._deep_progress: dict[int, int] = {}
        self._fast_failures = 0
        self._last_info = ""
        self.hotkeys = None
        self.timer = QTimer(self)
        self.timer.setInterval(max(50, int(1000 / max(0.5, settings.steno_fps))))
        self.timer.timeout.connect(self._tick)

    # ------------------------------------------------------------------ setup
    def _wire_controls(self) -> None:
        c = self.controls
        c.pick_region.connect(self.pick_region)
        c.toggle_live.connect(lambda: self.set_live(not self.live))
        c.analyse_now.connect(lambda: self.analyse_now("fast"))
        c.deep_now.connect(lambda: self.analyse_now("deep"))
        c.toggle_overlay.connect(self.toggle_overlay)
        c.toggle_details.connect(lambda: self.panel.setVisible(not self.panel.isVisible()))
        c.quit_requested.connect(self.quit)
        c.hint_changed.connect(self._set_hint)

    def _register_hotkeys(self) -> None:
        s = self.settings
        actions = {s.steno_hotkey_now: lambda: self.analyse_now("fast"),
                   s.steno_hotkey_deep: lambda: self.analyse_now("deep"),
                   s.steno_hotkey_region: self.pick_region,
                   s.steno_hotkey_overlay: self.toggle_overlay,
                   s.steno_hotkey_pause: lambda: self.set_live(not self.live)}
        self.hotkeys = make_global_hotkeys()
        if self.hotkeys is not None:              # Windows: work while WebEx has the focus
            QApplication.instance().installNativeEventFilter(self.hotkeys)
            for combo, action in actions.items():
                if combo.strip():
                    self.hotkeys.register(combo, action)
            return
        for combo, action in actions.items():     # elsewhere: only while one of our windows is focused
            if combo.strip():
                shortcut = QShortcut(QKeySequence(qt_sequence(combo)), self.controls)
                shortcut.setContext(Qt.ShortcutContext.ApplicationShortcut)
                shortcut.activated.connect(action)

    def start(self) -> None:
        self.engine.start()
        self.controls.place_default()
        self.controls.show()
        exclude_from_capture(self.controls)
        self._register_hotkeys()
        region = self.initial_region or self._load_region()
        if region is not None:
            screen = QGuiApplication.screenAt(region.center()) or QGuiApplication.primaryScreen()
            self.set_region(screen, region)
        else:
            self.pick_region()
        self.timer.start()
        self._update_status()

    def quit(self) -> None:
        self.timer.stop()
        self.engine.stop()
        if self.hotkeys is not None:
            self.hotkeys.unregister_all()
        for w in (self.overlay, self.controls, self.panel):
            w.close()
        QApplication.instance().quit()

    # ------------------------------------------------------------------ region
    def _region_file(self):
        return self.settings.data_dir / "steno_region.json"

    def _load_region(self) -> QRect | None:
        try:
            data = json.loads(self._region_file().read_text(encoding="utf-8"))
            rect = QRect(int(data["x"]), int(data["y"]), int(data["w"]), int(data["h"]))
        except (OSError, ValueError, KeyError, TypeError):
            return None
        screen = QGuiApplication.screenAt(rect.center())
        return rect if screen is not None and screen.geometry().intersects(rect) else None

    def pick_region(self) -> None:
        if self._selectors:
            return
        self.timer.stop()
        self.overlay.hide()
        for screen in QGuiApplication.screens():
            sel = RegionSelector(screen)
            sel.selected.connect(self._region_chosen)
            sel.cancelled.connect(self._close_selectors)
            sel.show()
            self._selectors.append(sel)
        if self._selectors:
            self._selectors[0].activateWindow()
            self._selectors[0].setFocus()

    def _close_selectors(self) -> None:
        for sel in self._selectors:
            sel.close()
        self._selectors = []
        if self.region is not None:
            self.overlay.show()
        self.timer.start()

    def _region_chosen(self, rect: QRect) -> None:
        screen = QGuiApplication.screenAt(rect.center()) or QGuiApplication.primaryScreen()
        self._close_selectors()
        self.set_region(screen, rect)
        try:
            self._region_file().write_text(json.dumps({"screen": screen.name(), "x": rect.x(), "y": rect.y(),
                                                       "w": rect.width(), "h": rect.height()}), encoding="utf-8")
        except OSError:
            log.warning("could not remember the region")

    def set_region(self, screen, rect: QRect) -> None:
        self.screen = screen
        self.region = rect.intersected(screen.geometry())
        self.gate.reset()
        self.overlay.set_region(self.region, screen.geometry())
        self.overlay.clear()
        self.overlay.show()
        self.excluded = exclude_from_capture(self.overlay)
        exclude_from_capture(self.panel)
        self._shown_frame, self._shown_stage = 0, ""
        log.info("watching region %s on %s (capture exclusion: %s)", self.region, screen.name(), self.excluded)
        self._update_status()

    # ------------------------------------------------------------------ capture loop
    def _grab(self) -> QImage | None:
        if self.screen is None or self.region is None:
            return None
        geo = self.screen.geometry()
        pix = self.screen.grabWindow(0, self.region.x() - geo.x(), self.region.y() - geo.y(),
                                     self.region.width(), self.region.height())
        img = pix.toImage()
        return None if img.isNull() else img

    def _tick(self) -> None:
        if not self.live or self.region is None or self._grabbing:
            return
        img = self._grab()
        if img is None:
            return
        sig = qimage_signature(img)
        self._last_image, self._last_sig = img, sig
        if self.excluded and self._shown_frame in self._frame_sigs:
            shown = self._frame_sigs[self._shown_frame]
            self.overlay.set_stale(changed_fraction(sig, shown) > 2 * self.gate.cfg.change_threshold)
        trigger = self.gate.update(sig, time.monotonic())
        if trigger is Trigger.FAST:
            self._analyse("fast", img, sig, cancel_deep=True)
        elif trigger is Trigger.DEEP:
            self._analyse("deep", img, sig)

    def analyse_now(self, stage: str) -> None:
        """Button/hotkey: analyse the current picture right away."""
        if stage not in self.stages or self.region is None:
            return
        img = self._grab()
        if img is None:
            return
        sig = qimage_signature(img)
        if stage == "fast":
            self.gate.mark_fast(sig, time.monotonic())
        else:
            self.gate.mark_deep(sig)
        self._analyse(stage, img, sig)

    def _analyse(self, stage: str, img: QImage, sig: np.ndarray, cancel_deep: bool = False) -> None:
        frame_id = next(self._frame_ids)
        self._frame_sigs[frame_id] = sig
        for old in [k for k in self._frame_sigs if k < frame_id - 40]:
            self._frame_sigs.pop(old, None)
            self._image_sizes.pop(old, None)
        if self.excluded:
            self.engine.submit(Frame(frame_id, qimage_to_pil(img)), stage, cancel_deep=cancel_deep)
            return
        # Fallback: our overlay is in the screen grab, so take a clean grab with our windows hidden.
        hidden = [w for w in (self.overlay, self.panel, self.controls)
                  if w.isVisible() and self.region is not None and w.frameGeometry().intersects(self.region)]
        for w in hidden:
            w.hide()
        self._grabbing = True

        def grab_clean() -> None:
            clean = self._grab()
            if clean is None:
                clean = img
            for w in hidden:
                w.show()
            self._grabbing = False
            self.gate.rebase_next()
            self.engine.submit(Frame(frame_id, qimage_to_pil(clean)), stage, cancel_deep=cancel_deep)

        QTimer.singleShot(90, grab_clean)

    # ------------------------------------------------------------------ engine events (GUI thread)
    def _on_event(self, ev: dict) -> None:
        kind, fid, stage = ev["type"], ev.get("frame_id", 0), ev.get("stage", "")
        if kind == "started":
            self._image_sizes[fid] = tuple(ev["image_size"])
            self._update_status(f"{STAGE_LABEL.get(stage, stage)} …")
        elif kind == "mark":
            if stage == "fast":
                if fid > self._shown_frame:       # first mark of a newer picture replaces the overlay
                    self._show(fid, "fast", [ev["mark"]], self._image_sizes.get(fid, (1, 1)), "", "⚡ schnell …")
                elif fid == self._shown_frame and self._shown_stage == "fast":
                    self.overlay.add_mark(ev["mark"])
                    self._after_overlay_change()
            else:
                self._deep_progress[fid] = self._deep_progress.get(fid, 0) + 1
                self._update_status(f"🔍 genau: {self._deep_progress[fid]} Zeichen gelesen …")
        elif kind == "done":
            self._on_done(ev["result"])
        elif kind == "error":
            if ev.get("budget"):
                if stage == "deep":
                    self.gate.cfg.deep_after_s = 0.0  # stop automatic deep calls for today
                else:
                    self.set_live(False)
            self._update_status(f"⚠ {ev['message']}")
        elif kind == "cancelled":
            self._deep_progress.pop(fid, None)

    def _on_done(self, result: StenoResult) -> None:
        self._deep_progress.pop(result.frame_id, None)
        self.panel.show_result(result)
        failed = bool(result.error) and not result.marks
        if result.stage == "fast":
            self._fast_failures = self._fast_failures + 1 if failed else 0
            if self._fast_failures >= MAX_FAST_FAILURES and self.live:
                self.set_live(False)
                self._update_status(f"⚠ {result.error} – pausiert (▶ Live zum Fortsetzen)")
                return
        if not failed and result.frame_id >= self._shown_frame:
            size = (result.image_width, result.image_height)
            self._show(result.frame_id, result.stage, result.marks, size, result.answer, headline(result))
        self._last_info = f"{STAGE_LABEL.get(result.stage, '')} {result.duration_s:.1f}s"
        self._update_status(f"⚠ {result.error}" if result.error else None)

    def _show(self, frame_id: int, stage: str, marks, image_size, answer: str, head: str) -> None:
        self._shown_frame, self._shown_stage = frame_id, stage
        self.overlay.show_marks(marks, image_size, answer=answer, headline=head)
        self._after_overlay_change()

    def _after_overlay_change(self) -> None:
        if not self.excluded:
            self.gate.rebase_next()               # our own drawing is not new content

    # ------------------------------------------------------------------ small actions
    def set_live(self, live: bool) -> None:
        self.live = live
        self.controls.set_live(live)
        if live:
            self._fast_failures = 0
            self.gate.reset()
        self.overlay.set_status("" if live else "⏸ pausiert")
        self._update_status()

    def toggle_overlay(self) -> None:
        visible = not self.overlay.isVisible()
        self.overlay.setVisible(visible and self.region is not None)
        self.controls.overlay_button.setChecked(visible)

    def _set_hint(self, text: str) -> None:
        self.hint = text.strip()

    def _update_status(self, message: str | None = None) -> None:
        state = "live" if self.live else "pausiert"
        budget = getattr(self.backend, "budget", None)
        spent = f"heute ${budget.spent_today():.3f}" if budget is not None else "Fake-Modus ($0)"
        parts = [message or state, self._last_info, spent]
        if self.region is not None and not self.excluded:
            parts.append("Overlay wird kurz ausgeblendet")
        self.controls.set_status(" · ".join(p for p in parts if p))


def run_live(settings: Settings, *, fake: bool = False, deep: bool = True, region: str | None = None) -> int:
    """Start the Qt app; returns its exit code."""
    from factcheck.steno.cli import build_backend  # cli imports this module lazily, so no import cycle

    app = QApplication.instance() or QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    app.setApplicationName("factcheck-steno")
    live = StenoLiveApp(settings, build_backend(settings, fake=fake), deep=deep, region=parse_region(region))
    live.start()
    return app.exec()
