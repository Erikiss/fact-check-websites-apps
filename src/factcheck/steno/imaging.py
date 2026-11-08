"""Prepare a captured frame for the vision model so the model's pixel coordinates map 1:1 onto it.

Local context: the exact resize rule the API applies (reference implementation from the vision docs), and PNG
encoding of the resized frame.
Global context: vision.py sends `PreparedImage.png`; the overlay maps the boxes the model returns from
`PreparedImage` pixels back to screen coordinates with `scale_to_source`. Pre-resizing ourselves means the API
never resizes again, so no coordinate drift (https://platform.claude.com/docs/en/build-with-claude/vision-coordinates).
"""

from __future__ import annotations

import hashlib
import io
import math
from dataclasses import dataclass

from PIL import Image

PATCH = 28                       # one visual token = one 28x28 pixel patch
STANDARD_TIER = (1568, 1568)     # (max long edge px, max visual tokens) for older models
HIGH_RES_TIER = (2576, 4784)     # Claude 4.7 and later (Haiku 5.5, Sonnet 5.5, Opus 5.5)


def count_image_tokens(width: int, height: int) -> int:
    """Visual tokens an image costs: ceil(w/28) * ceil(h/28)."""
    return math.ceil(width / PATCH) * math.ceil(height / PATCH)


def resized_size(width: int, height: int, max_edge: int = STANDARD_TIER[0],
                 max_tokens: int = STANDARD_TIER[1]) -> tuple[int, int]:
    """The largest aspect-preserving size that fits both limits (the rule the API itself applies).

    Ported from the reference implementation in the vision-coordinates docs; images that fit are unchanged.
    """

    def fits(w: int, h: int) -> bool:
        return (math.ceil(w / PATCH) * PATCH <= max_edge and math.ceil(h / PATCH) * PATCH <= max_edge
                and count_image_tokens(w, h) <= max_tokens)

    if fits(width, height):
        return width, height
    if height > width:                                   # solve on the long edge, then swap back
        resized_h, resized_w = resized_size(height, width, max_edge, max_tokens)
        return resized_w, resized_h
    aspect = width / height
    lo, hi = 1, width                                    # lo always fits, hi never does
    while lo + 1 < hi:
        mid = (lo + hi) // 2
        if fits(mid, max(round(mid / aspect), 1)):
            lo = mid
        else:
            hi = mid
    return lo, max(round(lo / aspect), 1)


@dataclass(frozen=True)
class PreparedImage:
    """The exact image the model sees, plus the size of the capture it came from."""

    png: bytes
    width: int                  # pixels the model sees; its coordinates refer to this grid
    height: int
    source_width: int           # captured frame size (physical pixels of the screen region)
    source_height: int
    tokens: int
    sha1: str

    def scale_to_source(self) -> tuple[float, float]:
        """Multiply model coordinates by these factors to get capture-pixel coordinates."""
        return self.source_width / self.width, self.source_height / self.height


def prepare_image(image: Image.Image, max_edge: int = STANDARD_TIER[0], max_tokens: int = STANDARD_TIER[1],
                  compress_level: int = 3) -> PreparedImage:
    """Resize (if needed) to fit the limits and encode as PNG.

    PNG keeps thin pen strokes intact (JPEG artefacts hurt line drawings); a low zlib level keeps encoding
    fast, and mostly-white whiteboards compress well anyway.
    """
    rgb = image.convert("RGB")
    w, h = resized_size(rgb.width, rgb.height, max_edge, max_tokens)
    sent = rgb if (w, h) == rgb.size else rgb.resize((w, h), Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    sent.save(buf, format="PNG", compress_level=compress_level)
    data = buf.getvalue()
    return PreparedImage(png=data, width=w, height=h, source_width=rgb.width, source_height=rgb.height,
                         tokens=count_image_tokens(w, h), sha1=hashlib.sha1(data).hexdigest()[:12])
