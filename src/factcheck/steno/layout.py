"""What each label says and where it goes, so labels cover neither each other nor the shorthand they describe.

Local context: a minimal rectangle type, a greedy placer (above, below, right, left, then further out) clamped
into the drawable bounds, and the label text/colours per mark kind.
Global context: shared by the Qt overlay (ui/overlay.py) and the PIL renderer (render.py); each measures its
own text and passes the label sizes in, so both draw the same layout.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Rect:
    x: float
    y: float
    w: float
    h: float

    @property
    def right(self) -> float:
        return self.x + self.w

    @property
    def bottom(self) -> float:
        return self.y + self.h

    def intersects(self, other: Rect) -> bool:
        return self.x < other.right and other.x < self.right and self.y < other.bottom and other.y < self.bottom

    def inside(self, bounds: Rect) -> bool:
        return self.x >= bounds.x and self.y >= bounds.y and self.right <= bounds.right and self.bottom <= bounds.bottom


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(v, hi)) if hi >= lo else lo


def place_labels(anchors: list[Rect], sizes: list[tuple[float, float]], bounds: Rect,
                 gap: float = 3.0) -> list[tuple[float, float]]:
    """Top-left corner for each label (same order as `anchors`).

    Tries positions around the anchor and keeps the first that stays clear of earlier labels and of every
    anchor box; if none is clear, uses the first candidate (labels have a translucent background).
    """
    placed: list[Rect] = []
    out: list[tuple[float, float]] = []
    for anchor, (w, h) in zip(anchors, sizes, strict=True):
        candidates = [
            (anchor.x, anchor.y - h - gap),                         # above
            (anchor.x, anchor.bottom + gap),                        # below
            (anchor.right + gap, anchor.y),                         # right
            (anchor.x - w - gap, anchor.y),                         # left
            (anchor.x, anchor.y - 2 * (h + gap)),                   # further above
            (anchor.x, anchor.bottom + h + 2 * gap),                # further below
        ]
        chosen: Rect | None = None
        for cx, cy in candidates:
            r = Rect(_clamp(cx, bounds.x, bounds.right - w), _clamp(cy, bounds.y, bounds.bottom - h), w, h)
            if not any(r.intersects(o) for o in placed) and not any(r.intersects(a) for a in anchors):
                chosen = r
                break
        if chosen is None:
            cx, cy = candidates[0]
            chosen = Rect(_clamp(cx, bounds.x, bounds.right - w), _clamp(cy, bounds.y, bounds.bottom - h), w, h)
        placed.append(chosen)
        out.append((chosen.x, chosen.y))
    return out


# ------------------------------------------------------------------------------------------- label text
KIND_COLOURS = {"word": "#1e8e3e", "error": "#d93025", "note": "#f29900"}   # green / red / amber
MAX_LINE = 52


def _short(text: str, limit: int = MAX_LINE) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def label_lines(mark) -> list[str]:
    """The text lines drawn next to a mark (a `models.Mark`), most important first."""
    conf = f" · {round(mark.confidence * 100)}%" if (mark.kind == "error" or mark.confidence < 0.75) else ""
    alts = f"oder: {' / '.join(mark.alternatives)}" if mark.alternatives else ""
    if mark.kind == "error":
        head = f"✗ {mark.text or '?'}" + (f" → {mark.intended}" if mark.intended else "")
        lines = [head + conf, mark.issue, f"richtig: {mark.fix}" if mark.fix else "", alts]
    elif mark.kind == "note":
        lines = [(mark.text or mark.issue) + conf, mark.issue if mark.text else ""]
    else:
        lines = [(mark.text or "?") + conf, alts]
    return [_short(line) for line in lines if line.strip()]
