"""Data shapes of a live steno analysis: the JSON schema the model must follow and the cleaned-up records.

Local context: `ANALYSIS_SCHEMA` (structured outputs; only features the API supports: no min/max constraints),
`Mark` (one read word / suspected error, with its box in model-image pixels) and `StenoResult` (one finished
analysis with timings and cost).
Global context: vision.py turns streamed JSON items into Marks via `Mark.from_raw`; the overlay, the panel,
the PIL renderer and the CLI only ever see these models.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

MarkKind = Literal["word", "error", "note"]
Stage = Literal["fast", "deep"]

# Property order = generation order: locate first, then read, then judge (a cheap "look before you judge").
ITEM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "box": {"type": "array", "items": {"type": "integer"},
                "description": "[x1, y1, x2, y2] in pixels of the image, tight around the sign/word"},
        "text": {"type": "string", "description": "longhand reading of what is actually written"},
        "alternatives": {"type": "array", "items": {"type": "string"}},
        "kind": {"type": "string", "enum": ["word", "error", "note"]},
        "issue": {"type": "string"},
        "intended": {"type": "string"},
        "fix": {"type": "string"},
        "spot": {"type": "array", "items": {"type": "integer"},
                 "description": "[x, y] of the exact faulty stroke, or [] if none"},
        "confidence": {"type": "number"},
    },
    "required": ["box", "text", "alternatives", "kind", "issue", "intended", "fix", "spot", "confidence"],
    "additionalProperties": False,
}

ANALYSIS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "items": {"type": "array", "items": ITEM_SCHEMA},
        "answer": {"type": "string"},
    },
    "required": ["items", "answer"],
    "additionalProperties": False,
}


def _clip(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


class Mark(BaseModel):
    """One thing to draw: a read word, a suspected error, or a note. Coordinates are model-image pixels."""

    kind: MarkKind
    box: tuple[float, float, float, float] | None = None
    text: str = ""
    alternatives: list[str] = []
    issue: str = ""
    intended: str = ""
    fix: str = ""
    spot: tuple[float, float] | None = None
    confidence: float = 0.5

    @classmethod
    def from_raw(cls, raw: dict[str, Any], width: int, height: int) -> Mark | None:
        """Validate one streamed item: clamp the box into the image, drop degenerate boxes, normalise fields.

        Returns None for items that carry nothing drawable or readable.
        """
        kind = raw.get("kind") if raw.get("kind") in ("word", "error", "note") else "note"
        box = None
        b = raw.get("box")
        if isinstance(b, list) and len(b) == 4 and all(isinstance(v, (int, float)) for v in b):
            x1, x2 = sorted((_clip(float(b[0]), 0, width), _clip(float(b[2]), 0, width)))
            y1, y2 = sorted((_clip(float(b[1]), 0, height), _clip(float(b[3]), 0, height)))
            if x2 - x1 >= 2 and y2 - y1 >= 2:
                box = (x1, y1, x2, y2)
        spot = None
        s = raw.get("spot")
        if isinstance(s, list) and len(s) == 2 and all(isinstance(v, (int, float)) for v in s):
            spot = (_clip(float(s[0]), 0, width), _clip(float(s[1]), 0, height))
        text = str(raw.get("text") or "").strip()
        issue = str(raw.get("issue") or "").strip()
        if box is None and not text and not issue:
            return None
        alts = [str(a).strip() for a in (raw.get("alternatives") or []) if str(a).strip() and str(a).strip() != text]
        try:
            conf = _clip(float(raw.get("confidence", 0.5)), 0.0, 1.0)
        except (TypeError, ValueError):
            conf = 0.5
        return cls(kind=kind, box=box, text=text, alternatives=alts[:3], issue=issue,
                   intended=str(raw.get("intended") or "").strip(), fix=str(raw.get("fix") or "").strip(),
                   spot=spot, confidence=conf)


class StenoResult(BaseModel):
    """One finished (or failed) analysis of one frame."""

    frame_id: int
    stage: Stage
    model: str
    marks: list[Mark] = []
    answer: str = ""
    image_width: int = 0                 # size of the image the model saw (mark coordinates refer to it)
    image_height: int = 0
    image_tokens: int = 0
    ttft_s: float | None = None          # time to the first streamed text
    first_mark_s: float | None = None    # time to the first drawable mark
    duration_s: float = 0.0
    cost_usd: float = 0.0
    usage: dict[str, Any] = {}
    stop_reason: str = ""
    served_by: str = ""                  # model that actually answered (differs after a refusal fallback)
    error: str | None = None
    cancelled: bool = False
    created_at: datetime = Field(default_factory=datetime.now)

    @property
    def errors(self) -> list[Mark]:
        return [m for m in self.marks if m.kind == "error"]

    def reading(self) -> str:
        """The read words in order, as one line of longhand."""
        return " ".join(m.text for m in self.marks if m.kind in ("word", "error") and m.text)
