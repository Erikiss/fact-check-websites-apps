"""Pull complete array elements out of a JSON document while it is still streaming in.

Local context: `ItemStream.feed(chunk)` scans only the new characters, tracks strings/escapes and nesting, and
returns every element of the top-level `"items"` array the moment its closing brace arrives.
Global context: the vision model answers with one structured-output JSON object (schema in models.py);
parsing it item by item lets the overlay draw the first word ~1 s after the request instead of waiting for
the whole answer. `final()` parses the complete document at the end.
"""

from __future__ import annotations

import json
from typing import Any


class ItemStream:
    def __init__(self, array_key: str = "items"):
        self.array_key = array_key
        self.text = ""
        self._i = 0                      # next character to scan
        self._stack: list[str] = []      # open containers: "{" or "["
        self._in_string = False
        self._escape = False
        self._expect_key = False         # inside an object, where the next string is a key
        self._key_start = -1             # index of the opening quote of the key being read
        self._last_key: str | None = None  # last key read in the top-level object
        self._array_depth: int | None = None  # stack size just inside the target array
        self._item_start = -1

    def feed(self, chunk: str) -> list[dict[str, Any]]:
        """Append `chunk`; return the array elements completed by it (objects only, in order)."""
        self.text += chunk
        out: list[dict[str, Any]] = []
        text = self.text
        for i in range(self._i, len(text)):
            ch = text[i]
            if self._in_string:
                if self._escape:
                    self._escape = False
                elif ch == "\\":
                    self._escape = True
                elif ch == '"':
                    self._in_string = False
                    if self._key_start >= 0:
                        if len(self._stack) == 1:
                            try:
                                self._last_key = json.loads(text[self._key_start:i + 1])
                            except ValueError:
                                self._last_key = None
                        self._key_start = -1
                continue
            if ch == '"':
                self._in_string = True
                if self._stack and self._stack[-1] == "{" and self._expect_key:
                    self._key_start = i
            elif ch == "{":
                if self._array_depth is not None and len(self._stack) == self._array_depth and self._item_start < 0:
                    self._item_start = i
                self._stack.append("{")
                self._expect_key = True
            elif ch == "[":
                is_target = (self._array_depth is None and len(self._stack) == 1 and self._stack[0] == "{"
                             and self._last_key == self.array_key)
                self._stack.append("[")
                if is_target:
                    self._array_depth = len(self._stack)
                self._expect_key = False
            elif ch in "}]":
                if self._stack:
                    self._stack.pop()
                if ch == "}" and self._item_start >= 0 and len(self._stack) == self._array_depth:
                    try:
                        item = json.loads(text[self._item_start:i + 1])
                    except ValueError:
                        item = None
                    if isinstance(item, dict):
                        out.append(item)
                    self._item_start = -1
                if ch == "]" and self._array_depth is not None and len(self._stack) == self._array_depth - 1:
                    self._array_depth = -1       # target array closed; never match again
                self._expect_key = False
            elif ch == ",":
                self._expect_key = bool(self._stack) and self._stack[-1] == "{"
            elif ch == ":":
                self._expect_key = False
        self._i = len(text)
        return out

    def final(self) -> dict[str, Any] | None:
        """The whole document parsed, or None if it is not (yet) valid JSON."""
        try:
            value = json.loads(self.text)
        except ValueError:
            return None
        return value if isinstance(value, dict) else None
