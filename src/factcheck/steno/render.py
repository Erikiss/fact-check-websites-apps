"""Draw a steno analysis onto an image with Pillow (for `factcheck-steno image`, tests and previews).

Local context: the same marks, colours, label text and label placement as the live Qt overlay, rendered into a
PNG with a margin around the picture for labels and a banner with the answer on top; plus a one-line text form
of a mark for terminal output.
Global context: cli.py writes `render_annotated(...)` next to the analysed screenshot; ui/overlay.py draws the
identical layout live (layout.py holds the shared parts).
"""

from __future__ import annotations

import io
from functools import lru_cache

from PIL import Image, ImageDraw, ImageFont

from factcheck.steno.imaging import PreparedImage
from factcheck.steno.layout import KIND_COLOURS, Rect, label_lines, place_labels
from factcheck.steno.models import Mark, StenoResult

_FONT_CANDIDATES = ("segoeui.ttf", "arial.ttf", "DejaVuSans.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                    "/System/Library/Fonts/Supplemental/Arial.ttf")


@lru_cache(maxsize=8)
def _font(size: int) -> ImageFont.ImageFont | ImageFont.FreeTypeFont:
    for name in _FONT_CANDIDATES:
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default(size=size)


def _rgba(hex_colour: str, alpha: int) -> tuple[int, int, int, int]:
    h = hex_colour.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), alpha


def _pil_safe(text: str) -> str:
    """Pillow has no font fallback: swap symbols that common fonts lack for Latin-1 look-alikes."""
    return text.replace("✗", "×")


def format_mark(mark: Mark) -> str:
    """One terminal line per mark."""
    box = "[" + ",".join(str(round(v)) for v in mark.box) + "]" if mark.box else "[ohne Box]"
    icon = {"word": "·", "error": "", "note": "!"}[mark.kind]          # error lines already start with ✗
    return " ".join(p for p in (icon, box, " | ".join(label_lines(mark))) if p)


def render_annotated(image: PreparedImage | Image.Image, result: StenoResult, scale: float = 1.0,
                     margin: int = 150) -> Image.Image:
    """The analysed picture with boxes, labels and the answer; mark coordinates are model-image pixels."""
    base = Image.open(io.BytesIO(image.png)) if isinstance(image, PreparedImage) else image
    base = base.convert("RGBA")
    if scale != 1.0:
        base = base.resize((round(base.width * scale), round(base.height * scale)), Image.Resampling.LANCZOS)
    sx = base.width / max(1, result.image_width or base.width)
    sy = base.height / max(1, result.image_height or base.height)
    side = margin // 3
    canvas = Image.new("RGBA", (base.width + 2 * side, base.height + 2 * margin), (32, 33, 36, 255))
    canvas.paste(base, (side, margin))
    layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    font, small = _font(15), _font(13)

    drawn = [m for m in result.marks if m.box is not None]
    anchors: list[Rect] = []
    sizes: list[tuple[float, float]] = []
    texts: list[list[str]] = []
    for m in drawn:
        x1, y1, x2, y2 = m.box  # type: ignore[misc]
        anchors.append(Rect(side + x1 * sx, margin + y1 * sy, (x2 - x1) * sx, (y2 - y1) * sy))
        lines = label_lines(m)
        widths = [draw.textlength(_pil_safe(line), font=font if i == 0 else small) for i, line in enumerate(lines)]
        texts.append(lines)
        sizes.append((max(widths) + 10, 19 * len(lines) + 6))
    positions = place_labels(anchors, sizes, Rect(0, 0, canvas.width, canvas.height))

    for m, a, (lx, ly), (lw, lh), lines in zip(drawn, anchors, positions, sizes, texts, strict=True):
        colour = KIND_COLOURS[m.kind]
        width = 4 if m.kind == "error" else 2
        draw.rectangle([a.x, a.y, a.right, a.bottom], outline=_rgba(colour, 255 if m.confidence >= 0.5 else 150),
                       width=width)
        if m.spot is not None:
            px, py = side + m.spot[0] * sx, margin + m.spot[1] * sy
            draw.ellipse([px - 9, py - 9, px + 9, py + 9], outline=_rgba(colour, 255), width=3)
        draw.rounded_rectangle([lx, ly, lx + lw, ly + lh], radius=5, fill=(20, 20, 20, 205),
                               outline=_rgba(colour, 255), width=1)
        for i, line in enumerate(lines):
            draw.text((lx + 5, ly + 3 + 19 * i), _pil_safe(line), font=font if i == 0 else small,
                      fill=_rgba(colour, 255) if i == 0 else (235, 235, 235, 255))

    stage = {"fast": "[schnell]", "deep": "[genau]"}.get(result.stage, result.stage)
    timing = f"{result.model} · {result.duration_s:.1f}s" + (f" · ${result.cost_usd:.4f}" if result.cost_usd else "")
    head = result.error or result.answer or "(keine Antwort)"
    draw.text((side, 10), f"{stage}  {timing}", font=small, fill=(180, 180, 180, 255))
    draw.text((side, 32), head[:110], font=_font(17), fill=(255, 255, 255, 255))
    if len(head) > 110:
        draw.text((side, 56), head[110:220], font=_font(17), fill=(255, 255, 255, 255))
    return Image.alpha_composite(canvas, layer).convert("RGB")
