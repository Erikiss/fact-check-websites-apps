"""The click-through overlay: boxes, readings, errors and the answer, drawn right over the watched region.

Local context: a frameless, translucent, always-on-top window that ignores the mouse (WindowTransparentForInput),
slightly larger than the region so labels and the answer banner can sit outside the shorthand. Mark boxes come
in model-image pixels and are scaled onto the region; label placement is shared with the PIL renderer.
Global context: ui/app.py feeds it marks as they stream in (`add_mark`), whole results (`show_marks`), status
text and a "stale" flag (the picture changed since these marks were made -> drawn faded).
"""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRect, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QFontMetricsF, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QWidget

from factcheck.steno.layout import KIND_COLOURS, Rect, label_lines, place_labels
from factcheck.steno.models import Mark


class OverlayWindow(QWidget):
    MARGIN = 230          # extra room around the region (clipped to the screen) for labels and the banner
    LINE_GAP = 2.0

    def __init__(self) -> None:
        flags = (Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.Tool
                 | Qt.WindowType.WindowTransparentForInput | Qt.WindowType.WindowDoesNotAcceptFocus)
        super().__init__(None, flags)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setWindowTitle("Steno-Overlay")
        self.region = QRect()                 # global logical coordinates of the watched region
        self.image_size = (1, 1)              # size of the image the marks' coordinates refer to
        self.marks: list[Mark] = []
        self.answer = ""
        self.headline = ""                    # stage · model · timing
        self.status = ""
        self.stale = False
        self.show_border = True
        self.font_main = QFont()
        self.font_main.setPointSizeF(10.5)
        self.font_main.setBold(True)
        self.font_small = QFont()
        self.font_small.setPointSizeF(9.5)
        self.font_banner = QFont()
        self.font_banner.setPointSizeF(11.5)
        self.font_banner.setBold(True)

    # ------------------------------------------------------------------ state
    def set_region(self, region: QRect, screen_geometry: QRect) -> None:
        self.region = QRect(region)
        m = self.MARGIN
        self.setGeometry(region.adjusted(-m, -m, m, m).intersected(screen_geometry))
        self.update()

    def show_marks(self, marks: list[Mark], image_size: tuple[int, int], answer: str = "", headline: str = "") -> None:
        self.marks = list(marks)
        self.image_size = (max(1, image_size[0]), max(1, image_size[1]))
        self.answer, self.headline, self.stale = answer, headline, False
        self.update()

    def add_mark(self, mark: Mark) -> None:
        self.marks.append(mark)
        self.update()

    def set_answer(self, answer: str, headline: str) -> None:
        self.answer, self.headline = answer, headline
        self.update()

    def set_status(self, text: str) -> None:
        if text != self.status:
            self.status = text
            self.update()

    def set_stale(self, stale: bool) -> None:
        if stale != self.stale:
            self.stale = stale
            self.update()

    def clear(self) -> None:
        self.marks, self.answer, self.headline, self.stale = [], "", "", False
        self.update()

    # ------------------------------------------------------------------ geometry helpers
    def _region_local(self) -> QRectF:
        top_left = self.region.topLeft() - self.geometry().topLeft()
        return QRectF(top_left.x(), top_left.y(), self.region.width(), self.region.height())

    def _map(self, x: float, y: float) -> QPointF:
        r = self._region_local()
        return QPointF(r.x() + x * r.width() / self.image_size[0], r.y() + y * r.height() / self.image_size[1])

    def _map_box(self, box: tuple[float, float, float, float]) -> QRectF:
        a, b = self._map(box[0], box[1]), self._map(box[2], box[3])
        return QRectF(a, b)

    # ------------------------------------------------------------------ painting
    def paintEvent(self, event) -> None:
        if self.region.isNull():
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        region = self._region_local()
        if self.show_border:
            pen = QPen(QColor(66, 133, 244, 170), 1.5, Qt.PenStyle.DashLine)
            p.setPen(pen)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRect(region.adjusted(-2, -2, 2, 2))
        if self.stale:
            p.setOpacity(0.4)
        self._paint_marks(p)
        p.setOpacity(1.0)
        self._paint_banner(p, region)
        if self.status:
            p.setFont(self.font_small)
            fm = QFontMetricsF(self.font_small)
            w = fm.horizontalAdvance(self.status) + 12
            box = QRectF(region.right() - w, region.bottom() + 4, w, fm.height() + 6)
            if box.bottom() > self.height():
                box.moveTop(region.bottom() - box.height() - 2)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(20, 20, 20, 190))
            p.drawRoundedRect(box, 5, 5)
            p.setPen(QColor(230, 230, 230))
            p.drawText(box, Qt.AlignmentFlag.AlignCenter, self.status)
        p.end()

    def _paint_marks(self, p: QPainter) -> None:
        drawn = [m for m in self.marks if m.box is not None]
        fm_main, fm_small = QFontMetricsF(self.font_main), QFontMetricsF(self.font_small)
        anchors, sizes, texts = [], [], []
        for m in drawn:
            r = self._map_box(m.box)  # type: ignore[arg-type]
            anchors.append(Rect(r.x(), r.y(), r.width(), r.height()))
            lines = label_lines(m)
            widths = [(fm_main if i == 0 else fm_small).horizontalAdvance(t) for i, t in enumerate(lines)]
            heights = [(fm_main if i == 0 else fm_small).height() + self.LINE_GAP for i in range(len(lines))]
            texts.append(lines)
            sizes.append((max(widths) + 12, sum(heights) + 6))
        positions = place_labels(anchors, sizes, Rect(0, 0, self.width(), self.height()))
        for m, a, (lx, ly), (lw, lh), lines in zip(drawn, anchors, positions, sizes, texts, strict=True):
            colour = QColor(KIND_COLOURS[m.kind])
            pen = QPen(colour, 3.0 if m.kind == "error" else 2.0)
            if m.confidence < 0.5:
                pen.setStyle(Qt.PenStyle.DashLine)
            p.setPen(pen)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(QRectF(a.x, a.y, a.w, a.h), 4, 4)
            if m.spot is not None:
                c = self._map(*m.spot)
                p.setPen(QPen(colour, 2.5))
                p.drawEllipse(c, 9, 9)
                p.drawLine(QPointF(c.x() - 14, c.y()), QPointF(c.x() - 9, c.y()))
            label = QRectF(lx, ly, lw, lh)
            path = QPainterPath()
            path.addRoundedRect(label, 5, 5)
            p.fillPath(path, QColor(18, 18, 18, 210))
            p.setPen(QPen(colour, 1.2))
            p.drawPath(path)
            y = ly + 3
            for i, text in enumerate(lines):
                font, fm = (self.font_main, fm_main) if i == 0 else (self.font_small, fm_small)
                p.setFont(font)
                p.setPen(colour.lighter(135) if i == 0 else QColor(236, 236, 236))
                p.drawText(QPointF(lx + 6, y + fm.ascent()), text)
                y += fm.height() + self.LINE_GAP

    def _paint_banner(self, p: QPainter, region: QRectF) -> None:
        text = self.answer or ""
        if not (text or self.headline):
            return
        fm = QFontMetricsF(self.font_banner)
        fm_small = QFontMetricsF(self.font_small)
        h = fm.height() + fm_small.height() + 12
        top = region.top() - h - 6 if region.top() - h - 6 >= 0 else region.top() + 4
        box = QRectF(region.left(), top, max(region.width(), 360.0), h)
        if box.right() > self.width():
            box.moveRight(self.width() - 2)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(18, 18, 18, 215))
        p.drawRoundedRect(box, 6, 6)
        p.setFont(self.font_small)
        p.setPen(QColor(170, 190, 255))
        p.drawText(QPointF(box.left() + 8, box.top() + 4 + fm_small.ascent()), self.headline)
        p.setFont(self.font_banner)
        p.setPen(QColor(255, 255, 255))
        elided = fm.elidedText(text, Qt.TextElideMode.ElideRight, box.width() - 16)
        p.drawText(QPointF(box.left() + 8, box.top() + 6 + fm_small.height() + fm.ascent()), elided)
