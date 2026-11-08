"""System prompt and per-frame user text for the live steno analysis.

Local context: one static German system prompt (template knowledge/system_prompt.md with the DEK knowledge from
knowledge/dek.md or the file named by STENO_KNOWLEDGE_FILE) and a tiny per-frame user text (image size + hint).
The Android app (android/) bundles the same knowledge/ files, so desktop and tablet send identical prompts.
Global context: vision.py marks the system prompt with `cache_control`, so it must stay byte-identical between
frames — no timestamps or per-frame values in it; everything that changes goes into `build_user_text`.
Output field rules mirror `models.ANALYSIS_SCHEMA`; coordinate wording follows the vision docs' advice to ask
for absolute pixel boxes `[x1, y1, x2, y2]` (https://platform.claude.com/docs/en/build-with-claude/vision-coordinates).
"""

from __future__ import annotations

import re
from pathlib import Path

KNOWLEDGE_DIR = Path(__file__).with_name("knowledge")
DEFAULT_KNOWLEDGE = KNOWLEDGE_DIR / "dek.md"
# Shared with the Android app (android/ reads the same files as assets), so both send the same prompt.
SYSTEM_TEMPLATE = (KNOWLEDGE_DIR / "system_prompt.md").read_text(encoding="utf-8").strip()


def load_knowledge(path: str | Path | None = None) -> str:
    """The DEK knowledge text: the user's file if given, else the bundled knowledge/dek.md.

    HTML comments (`<!-- ... -->`) are notes for the person editing the file and are left out of the prompt.
    """
    target = Path(path).expanduser() if path else DEFAULT_KNOWLEDGE
    text = re.sub(r"<!--.*?-->", "", target.read_text(encoding="utf-8"), flags=re.DOTALL)
    return text.strip()


def build_system_prompt(knowledge: str) -> str:
    return SYSTEM_TEMPLATE.replace("{knowledge}", knowledge.strip())


def build_user_text(width: int, height: int, hint: str = "") -> str:
    """Per-frame text sent after the image (kept out of the cached system prompt)."""
    text = f"Bildgröße: {width}×{height} Pixel."
    if hint.strip():
        text += f"\nHinweis des Nutzers: {hint.strip()}"
    return text
