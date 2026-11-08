"""Windows-specific helpers: keep our windows out of screen captures, and system-wide hotkeys.

Local context: `exclude_from_capture` calls SetWindowDisplayAffinity(WDA_EXCLUDEFROMCAPTURE) (Windows 10 2004+)
so screenshots/screen grabs - ours included - do not contain the window; `GlobalHotkeys` registers hotkeys
with RegisterHotKey and receives WM_HOTKEY through a Qt native event filter; `parse_hotkey` turns "ctrl+alt+s"
into Win32 modifier flags and a virtual-key code (pure, testable everywhere).
Docs: https://learn.microsoft.com/windows/win32/api/winuser/nf-winuser-setwindowdisplayaffinity and
https://learn.microsoft.com/windows/win32/api/winuser/nf-winuser-registerhotkey
Global context: ui/app.py uses these when running on Windows; on other systems it falls back to hiding the
overlay while grabbing frames for the model and to in-app shortcuts.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Callable

log = logging.getLogger(__name__)

WDA_EXCLUDEFROMCAPTURE = 0x00000011
WM_HOTKEY = 0x0312
MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN, MOD_NOREPEAT = 0x0001, 0x0002, 0x0004, 0x0008, 0x4000
_MODIFIERS = {"alt": MOD_ALT, "ctrl": MOD_CONTROL, "control": MOD_CONTROL, "strg": MOD_CONTROL, "shift": MOD_SHIFT,
              "win": MOD_WIN, "meta": MOD_WIN}
_NAMED_KEYS = {"space": 0x20, "enter": 0x0D, "tab": 0x09, "esc": 0x1B, "escape": 0x1B, "pause": 0x13,
               "insert": 0x2D, "delete": 0x2E, "home": 0x24, "end": 0x23, "pageup": 0x21, "pagedown": 0x22}


def parse_hotkey(combo: str) -> tuple[int, int]:
    """'ctrl+alt+s' -> (MOD_CONTROL | MOD_ALT, ord('S')). Raises ValueError for unknown keys."""
    parts = [p.strip().lower() for p in combo.replace("-", "+").split("+") if p.strip()]
    if not parts:
        raise ValueError("empty hotkey")
    mods = 0
    for p in parts[:-1]:
        if p not in _MODIFIERS:
            raise ValueError(f"unknown modifier {p!r} in {combo!r}")
        mods |= _MODIFIERS[p]
    key = parts[-1]
    if len(key) == 1 and key.isalnum():
        vk = ord(key.upper())
    elif key in _NAMED_KEYS:
        vk = _NAMED_KEYS[key]
    elif key.startswith("f") and key[1:].isdigit() and 1 <= int(key[1:]) <= 24:
        vk = 0x70 + int(key[1:]) - 1
    else:
        raise ValueError(f"unknown key {key!r} in {combo!r}")
    return mods, vk


def qt_sequence(combo: str) -> str:
    """'ctrl+alt+s' -> 'Ctrl+Alt+S' (QKeySequence text) for the non-Windows fallback shortcuts."""
    names = {"ctrl": "Ctrl", "control": "Ctrl", "strg": "Ctrl", "alt": "Alt", "shift": "Shift", "win": "Meta",
             "meta": "Meta"}
    parts = [p.strip().lower() for p in combo.replace("-", "+").split("+") if p.strip()]
    return "+".join(names.get(p, p.upper() if len(p) == 1 else p.capitalize()) for p in parts)


def exclude_from_capture(widget) -> bool:
    """Hide a top-level Qt window from all screen captures. True if Windows accepted it."""
    if sys.platform != "win32":
        return False
    import ctypes

    try:
        hwnd = int(widget.winId())
        ok = bool(ctypes.windll.user32.SetWindowDisplayAffinity(ctypes.c_void_p(hwnd), WDA_EXCLUDEFROMCAPTURE))
    except Exception:                                    # very old Windows or no native window yet
        log.exception("SetWindowDisplayAffinity failed")
        return False
    if not ok:
        log.warning("SetWindowDisplayAffinity(WDA_EXCLUDEFROMCAPTURE) refused (needs Windows 10 2004 or later)")
    return ok


def make_global_hotkeys():
    """A GlobalHotkeys instance on Windows, else None (the caller uses in-app shortcuts)."""
    if sys.platform != "win32":
        return None
    import ctypes
    from ctypes import wintypes

    from PySide6.QtCore import QAbstractNativeEventFilter

    class GlobalHotkeys(QAbstractNativeEventFilter):
        """RegisterHotKey(NULL, ...) posts WM_HOTKEY to this thread's queue; Qt passes it to native filters."""

        def __init__(self) -> None:
            super().__init__()
            self._callbacks: dict[int, Callable[[], None]] = {}
            self._next_id = 0xB000

        def register(self, combo: str, callback: Callable[[], None]) -> bool:
            try:
                mods, vk = parse_hotkey(combo)
            except ValueError as exc:
                log.warning("hotkey %r ignored: %s", combo, exc)
                return False
            hotkey_id = self._next_id
            self._next_id += 1
            if not ctypes.windll.user32.RegisterHotKey(None, hotkey_id, mods | MOD_NOREPEAT, vk):
                log.warning("hotkey %s is already used by another program", combo)
                return False
            self._callbacks[hotkey_id] = callback
            return True

        def unregister_all(self) -> None:
            for hotkey_id in list(self._callbacks):
                ctypes.windll.user32.UnregisterHotKey(None, hotkey_id)
            self._callbacks.clear()

        def nativeEventFilter(self, event_type, message):
            if bytes(event_type) == b"windows_generic_MSG":
                msg = wintypes.MSG.from_address(int(message))
                if msg.message == WM_HOTKEY and msg.wParam in self._callbacks:
                    self._callbacks[msg.wParam]()
                    return True, 0
            return False, 0

    return GlobalHotkeys()
