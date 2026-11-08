"""Steno Qt windows, headless (offscreen platform): overlay painting, conversions, region picker, live loop ($0)."""

from __future__ import annotations

import os
import time

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
QtWidgets = pytest.importorskip("PySide6.QtWidgets", exc_type=ImportError)

from PySide6.QtCore import QPoint, QRect, Qt
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtTest import QTest

from factcheck.steno.models import Mark
from factcheck.steno.ui.app import StenoLiveApp, parse_region, qimage_signature, qimage_to_pil
from factcheck.steno.ui.overlay import OverlayWindow
from factcheck.steno.ui.widgets import RegionSelector
from factcheck.steno.vision import FakeVision


@pytest.fixture(scope="module")
def qapp():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    app.setQuitOnLastWindowClosed(False)
    return app


def whiteboard(w=400, h=200) -> QImage:
    img = QImage(w, h, QImage.Format.Format_RGB32)
    img.fill(QColor("white"))
    p = QPainter(img)
    p.setPen(QColor("black"))
    for x in (40, 160, 280):
        p.drawLine(x, 60, x + 30, 140)
        p.drawLine(x + 30, 140, x + 60, 70)
    p.end()
    return img


def wait_until(predicate, timeout=6.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        QTest.qWait(20)
    return False


def test_qimage_conversions(qapp):
    img = whiteboard()
    pil = qimage_to_pil(img)
    assert pil.size == (400, 200) and pil.getpixel((0, 0)) == (255, 255, 255)
    sig = qimage_signature(img)
    assert sig.shape == (80, 160) and sig.dtype == np.float32 and sig.min() < 250
    assert parse_region("10,20,300,200") == QRect(10, 20, 300, 200)


def test_overlay_draws_marks_scaled_onto_the_region(qapp):
    ov = OverlayWindow()
    ov.set_region(QRect(300, 300, 400, 200), QRect(0, 0, 800, 800))
    assert ov.geometry() == QRect(70, 70, 730, 660)                     # region + margin, clipped to the screen
    ov.show_marks([Mark(kind="error", box=(100, 50, 200, 100), text="Hans", issue="zu rund", confidence=0.9)],
                  image_size=(800, 400), answer="Fehler im zweiten Wort", headline="⚡ schnell")
    shot = ov.grab().toImage()
    # model box (100,50)-(200,100) on an 800x400 image -> region (50,25)-(100,50) -> window offset (230,230)
    edge = shot.pixelColor(230 + 75, 230 + 25)
    assert edge.alpha() > 150 and edge.red() > 150 and edge.green() < 120      # red error frame
    assert shot.pixelColor(5, 5).alpha() == 0                                  # rest stays transparent
    ov.close()


def test_region_selector_emits_global_rectangle(qapp):
    screen = qapp.primaryScreen()
    sel = RegionSelector(screen)
    sel.show()
    got = []
    sel.selected.connect(got.append)
    QTest.mousePress(sel, Qt.MouseButton.LeftButton, pos=QPoint(100, 120))
    QTest.mouseMove(sel, QPoint(300, 260))
    QTest.mouseRelease(sel, Qt.MouseButton.LeftButton, pos=QPoint(300, 260))
    assert got == [QRect(QPoint(100, 120), QPoint(300, 260)).translated(screen.geometry().topLeft())]
    sel.close()


@pytest.mark.parametrize("excluded", [True, False])
def test_live_loop_draws_fast_then_deep(qapp, settings, excluded):
    """Frames come from a QImage instead of the screen; FakeVision answers. `excluded=False` is the
    non-Windows path (overlay hidden while grabbing, gate re-based after repaints)."""
    settings.steno_fps, settings.steno_settle_s, settings.steno_min_interval_s = 20.0, 0.1, 0.1
    settings.steno_deep_after_s = 0.5
    board = whiteboard()
    live = StenoLiveApp(settings, FakeVision(delay_s=0.01), deep=True, region=QRect(100, 100, 400, 200))
    live._grab = lambda: board.copy()
    try:
        live.start()
        live.excluded = excluded
        assert wait_until(lambda: live._shown_stage == "fast" and len(live.overlay.marks) == 3)
        assert wait_until(lambda: live._shown_stage == "deep" and live.overlay.answer.startswith("FAKE"))
        assert live.overlay.image_size == (400, 200)
        assert "deep" in live.panel.latest and "fast" in live.panel.latest
        live.set_live(False)
        assert live.overlay.status == "⏸ pausiert" and live.controls.live_button.text() == "▶ Live"
    finally:
        live.timer.stop()
        live.engine.stop()
        for w in (live.overlay, live.controls, live.panel):
            w.close()
