"""Decide *when* a captured frame is worth sending to the model (pure functions + a small state machine).

Local context: tiny grayscale signatures of each capture tick, a changed-pixel metric, and `ChangeGate`, which
turns a stream of signatures into FAST / DEEP triggers:
  * FAST when the picture differs from the last analysed one and has stopped moving for `settle_s`
    (the lecturer finished a stroke), or has kept changing for `max_wait_s` (stay live while they draw);
  * DEEP once per distinct picture that stayed still for `deep_after_s` (a slower, stronger second opinion).
A blank picture (empty board, black screen) never triggers anything.
Global context: the Qt capture loop (ui/app.py) feeds `update()` on every tick; manual hotkeys call
`mark_fast` / `mark_deep` so the gate does not immediately re-trigger. Time is passed in, so tests need no clock.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum

import numpy as np


def signature(pixels: np.ndarray, target_width: int = 160) -> np.ndarray:
    """Block-average a grayscale (H, W) or colour (H, W, C) uint8 array down to about `target_width` columns."""
    a = np.asarray(pixels, dtype=np.float32)
    if a.ndim == 3:
        a = a[..., :3].mean(axis=2)                      # plain mean is enough for change detection
    h, w = a.shape
    f = max(1, w // max(1, target_width))
    if f > 1:
        hh, ww = (h // f) * f, (w // f) * f
        if hh and ww:
            a = a[:hh, :ww].reshape(hh // f, f, ww // f, f).mean(axis=(1, 3))
    return a


def changed_fraction(a: np.ndarray, b: np.ndarray, pixel_threshold: float = 24.0) -> float:
    """Share of signature pixels whose brightness differs by more than `pixel_threshold` (0..255 scale).

    Block averaging plus the threshold ignores video-compression shimmer from the conference stream.
    """
    if a.shape != b.shape:
        return 1.0
    return float(np.mean(np.abs(a - b) > pixel_threshold))


class Trigger(str, Enum):
    NONE = "none"
    FAST = "fast"
    DEEP = "deep"


@dataclass
class GateConfig:
    settle_s: float = 0.45          # still this long after a change -> fast analysis
    max_wait_s: float = 2.0         # changing continuously -> fast analysis at least this often
    min_interval_s: float = 0.8     # never start fast analyses closer together than this
    change_threshold: float = 0.004  # share of pixels that must differ from the last analysed picture
    motion_threshold: float = 0.002  # share of pixels that differ between ticks = "still moving"
    deep_after_s: float = 2.5       # still this long -> one deep analysis of this picture (<= 0: never)
    pixel_threshold: float = 24.0
    max_backoff_steps: int = 3      # constant motion (e.g. a webcam in the region): 2s, 4s, 8s, 16s
    blank_std: float = 1.5          # a picture this uniform (empty whiteboard, black screen) is never sent


class ChangeGate:
    """Turns per-tick signatures into FAST / DEEP triggers (see module docstring)."""

    def __init__(self, cfg: GateConfig | None = None):
        self.cfg = cfg or GateConfig()
        self.reset()

    def reset(self) -> None:
        self._prev: np.ndarray | None = None       # previous tick
        self._ref: np.ndarray | None = None        # picture of the last fast analysis
        self._deep_ref: np.ndarray | None = None   # picture of the last deep analysis
        self._still_since: float | None = None
        self._dirty_since: float | None = None
        self._last_fast = -math.inf
        self._overdue_streak = 0
        self._rebase_next = False

    def _diff(self, a: np.ndarray, b: np.ndarray) -> float:
        return changed_fraction(a, b, self.cfg.pixel_threshold)

    def update(self, sig: np.ndarray, now: float) -> Trigger:
        c = self.cfg
        if self._rebase_next:                      # our own overlay just changed the picture (fallback mode)
            self._rebase_next = False
            if self._ref is not None and self._deep_ref is not None and self._diff(self._ref, self._deep_ref) <= c.change_threshold:
                self._deep_ref = sig
            self._prev = self._ref = sig
            return Trigger.NONE

        moving = self._prev is not None and self._diff(sig, self._prev) > c.motion_threshold
        self._prev = sig
        if moving:
            self._still_since = None
        elif self._still_since is None:
            self._still_since = now

        if float(sig.std()) < c.blank_std:          # nothing written: nothing to analyse (and nothing to pay for)
            self._ref, self._dirty_since = sig, None
            return Trigger.NONE

        dirty = self._ref is None or self._diff(sig, self._ref) > c.change_threshold
        if not dirty:
            self._dirty_since = None
        elif self._dirty_since is None:
            self._dirty_since = now

        if dirty and now - self._last_fast >= c.min_interval_s:
            settled = self._still_since is not None and now - self._still_since >= c.settle_s
            max_wait = c.max_wait_s * 2 ** min(self._overdue_streak, c.max_backoff_steps)
            overdue = self._dirty_since is not None and now - self._dirty_since >= max_wait
            if settled or overdue:
                self._overdue_streak = 0 if settled else self._overdue_streak + 1
                self.mark_fast(sig, now)
                return Trigger.FAST

        if (not dirty and c.deep_after_s > 0 and self._still_since is not None
                and now - self._still_since >= c.deep_after_s
                and (self._deep_ref is None or self._diff(sig, self._deep_ref) > c.change_threshold)):
            self.mark_deep(sig)
            return Trigger.DEEP
        return Trigger.NONE

    def mark_fast(self, sig: np.ndarray, now: float) -> None:
        """Record that `sig` was (or is being) analysed by the fast stage."""
        self._ref = sig
        self._dirty_since = None
        self._last_fast = now

    def mark_deep(self, sig: np.ndarray) -> None:
        """Record that `sig` was (or is being) analysed by the deep stage."""
        self._deep_ref = sig

    def rebase_next(self) -> None:
        """Fallback capture mode: accept the next tick as the new reference without triggering."""
        self._rebase_next = True
