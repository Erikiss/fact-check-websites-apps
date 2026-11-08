"""The interactive windows of the live steno mode: region picker, control bar and details panel.

Local context:
  * RegionSelector - one translucent full-screen window per monitor; drag a rectangle, Esc cancels.
  * ControlBar     - small always-on-top bar (drag to move): region, live/pause, analyse now, deep now,
                     overlay on/off, details, quit, a hint field for the model, and a status line.
  * DetailsPanel   - the latest fast and deep results as text (reading, errors, alternatives, answer).
Global context: ui/app.py wires their signals to the capture loop and the engine; the overlay itself is
click-through (ui/overlay.py), so every interaction goes through these windows or the hotkeys.
"""

from __future__ import annotations

import html

from PySide6.QtCore import QPoint, QRect, Qt, Signal
from PySide6.QtGui import QColor, QFont, QGuiApplication, QPainter, QPen
from PySide6.QtWidgets import QHBoxLayout, QLabel, QLineEdit, QTextBrowser, QToolButton, QVBoxLayout, QWidget

from factcheck.steno.models import StenoResult


class RegionSelector(QWidget):
    """Full-screen picker for one monitor; emits the chosen rectangle in global logical coordinates."""

    selected = Signal(QRect)
    cancelled = Signal()

    def __init__(self, screen) -> None:
        super().__init__(None, Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint
                         | Qt.WindowType.Tool)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setScreen(screen)
        self.setGeometry(screen.geometry())
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._origin: QPoint | None = None
        self._current: QPoint | None = None

    def _rect(self) -> QRect | None:
        if self._origin is None or self._current is None:
            return None
        return QRect(self._origin, self._current).normalized()

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(0, 0, 0, 110))
        r = self._rect()
        if r is not None:
            p.setCompositionMode(QPainter.CompositionMode.CompositionMode_Source)
            p.fillRect(r, QColor(0, 0, 0, 1))          # alpha 1, not 0: fully clear pixels would let clicks through
            p.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
            p.setPen(QPen(QColor(66, 133, 244), 2))
            p.drawRect(r)
            p.setPen(QColor(255, 255, 255))
            p.drawText(r.adjusted(4, -22, 200, 0).topLeft() + QPoint(0, 14), f"{r.width()} × {r.height()}")
        font = QFont()
        font.setPointSize(13)
        p.setFont(font)
        p.setPen(QColor(255, 255, 255))
        p.drawText(self.rect().adjusted(0, 40, 0, 0), Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop,
                   "Bereich mit der Steno-Schrift aufziehen (z. B. das WebEx-Whiteboard) – Esc bricht ab")
        p.end()

    def mousePressEvent(self, event) -> None:
        self._origin = self._current = event.position().toPoint()
        self.update()

    def mouseMoveEvent(self, event) -> None:
        if self._origin is not None:
            self._current = event.position().toPoint()
            self.update()

    def mouseReleaseEvent(self, event) -> None:
        self._current = event.position().toPoint()
        r = self._rect()
        self._origin = self._current = None
        if r is not None and r.width() >= 24 and r.height() >= 24:
            self.selected.emit(r.translated(self.geometry().topLeft()))
        else:
            self.update()

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_Escape:
            self.cancelled.emit()


BAR_STYLE = """
QWidget#bar { background: rgba(28, 28, 30, 235); border-radius: 9px; }
QToolButton { color: #f1f3f4; background: #3c4043; border: none; border-radius: 6px; padding: 4px 8px; }
QToolButton:hover { background: #5f6368; }
QToolButton:checked { background: #1a73e8; }
QLineEdit { color: #f1f3f4; background: #202124; border: 1px solid #5f6368; border-radius: 6px; padding: 3px 6px; }
QLabel { color: #bdc1c6; }
QLabel#grip { color: #8ab4f8; font-weight: bold; padding: 0 4px; }
"""


class ControlBar(QWidget):
    pick_region = Signal()
    toggle_live = Signal()
    analyse_now = Signal()
    deep_now = Signal()
    toggle_overlay = Signal()
    toggle_details = Signal()
    quit_requested = Signal()
    hint_changed = Signal(str)

    def __init__(self, hint: str = "", deep_available: bool = True) -> None:
        super().__init__(None, Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint
                         | Qt.WindowType.Tool)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setWindowTitle("Steno-Live")
        inner = QWidget(self)
        inner.setObjectName("bar")
        inner.setStyleSheet(BAR_STYLE)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(inner)
        col = QVBoxLayout(inner)
        col.setContentsMargins(8, 6, 8, 6)
        col.setSpacing(4)
        row = QHBoxLayout()
        row.setSpacing(5)
        grip = QLabel("⠿ Steno")
        grip.setObjectName("grip")
        row.addWidget(grip)

        def button(text: str, tip: str, signal, checkable: bool = False) -> QToolButton:
            b = QToolButton()
            b.setText(text)
            b.setToolTip(tip)
            b.setCheckable(checkable)
            b.clicked.connect(lambda *_: signal.emit())
            row.addWidget(b)
            return b

        button("⬚ Bereich", "Bereich neu wählen", self.pick_region)
        self.live_button = button("⏸ Pause", "Live-Analyse pausieren/fortsetzen", self.toggle_live)
        button("⚡ Jetzt", "Sofort schnell analysieren", self.analyse_now)
        self.deep_button = button("🔍 Genau", "Gründlich mit dem stärkeren Modell prüfen", self.deep_now)
        self.deep_button.setEnabled(deep_available)
        self.overlay_button = button("👁 Overlay", "Overlay ein/aus", self.toggle_overlay, checkable=True)
        self.overlay_button.setChecked(True)
        button("☰ Details", "Details-Fenster ein/aus", self.toggle_details)
        button("✕", "Beenden", self.quit_requested)
        col.addLayout(row)
        self.hint = QLineEdit(hint)
        self.hint.setPlaceholderText("Hinweis ans Modell, z. B. „Finde den Fehler“ oder „Eilschrift“")
        self.hint.editingFinished.connect(lambda: self.hint_changed.emit(self.hint.text()))
        col.addWidget(self.hint)
        self.status = QLabel("bereit")
        col.addWidget(self.status)
        self._drag_from: QPoint | None = None

    def set_live(self, live: bool) -> None:
        self.live_button.setText("⏸ Pause" if live else "▶ Live")

    def set_status(self, text: str) -> None:
        self.status.setText(text)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_from = event.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, event) -> None:
        if self._drag_from is not None:
            self.move(event.globalPosition().toPoint() - self._drag_from)

    def mouseReleaseEvent(self, event) -> None:
        self._drag_from = None

    def place_default(self) -> None:
        """Top centre of the primary screen."""
        screen = QGuiApplication.primaryScreen().availableGeometry()
        self.adjustSize()
        self.move(screen.center().x() - self.width() // 2, screen.top() + 8)


def result_html(result: StenoResult) -> str:
    """One result as HTML for the details panel."""
    e = html.escape
    stage = "🔍 genau" if result.stage == "deep" else "⚡ schnell"
    parts = [(f"<h3 style='margin:4px 0'>{stage} <small style='color:#888'>{e(result.served_by or result.model)} · "
              f"{result.duration_s:.1f}s · ${result.cost_usd:.4f} · {result.created_at:%H:%M:%S}</small></h3>")]
    if result.error:
        parts.append(f"<p style='color:#d93025'>⚠ {e(result.error)}</p>")
    if result.answer:
        parts.append(f"<p><b>{e(result.answer)}</b></p>")
    reading = result.reading()
    if reading:
        parts.append(f"<p>Übertragung: <i>{e(reading)}</i></p>")
    items = []
    for m in result.marks:
        alts = f" <span style='color:#888'>(oder: {e(' / '.join(m.alternatives))})</span>" if m.alternatives else ""
        conf = f" <span style='color:#888'>{round(m.confidence * 100)}%</span>"
        if m.kind == "error":
            detail = " – ".join(e(x) for x in (m.issue, f"richtig: {m.fix}" if m.fix else "") if x)
            meant = f" → {e(m.intended)}" if m.intended else ""
            items.append(f"<li style='color:#d93025'><b>✗ {e(m.text or '?')}{meant}</b>{conf}<br>{detail}{alts}</li>")
        elif m.kind == "note":
            items.append(f"<li style='color:#b06000'>{e(m.text or m.issue)}</li>")
        else:
            items.append(f"<li>{e(m.text or '?')}{conf}{alts}</li>")
    if items:
        parts.append("<ul style='margin-top:2px'>" + "".join(items) + "</ul>")
    return "".join(parts)


class DetailsPanel(QWidget):
    """Latest fast and deep result in one scrollable text view, newest picture first."""

    def __init__(self) -> None:
        super().__init__(None, Qt.WindowType.Tool | Qt.WindowType.WindowStaysOnTopHint)
        self.setWindowTitle("Steno – Details")
        self.view = QTextBrowser()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.addWidget(self.view)
        self.resize(460, 520)
        self.latest: dict[str, StenoResult] = {}

    def show_result(self, result: StenoResult) -> None:
        self.latest[result.stage] = result
        ordered = sorted(self.latest.values(), key=lambda r: r.frame_id, reverse=True)
        self.view.setHtml("<hr>".join(result_html(r) for r in ordered))
