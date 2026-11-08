"""LiveEngine: runs steno analyses on a background asyncio loop and reports progress as plain events.

Local context: thread-safe `submit(frame, stage)`; per stage a bounded number of calls in flight, where the
newest frame wins (a waiting fast frame is replaced by a newer one; a new deep request cancels the old one),
and fresh content cancels a deep look at the old picture. Image preparation runs in a worker thread.
Global context: the Qt app (ui/app.py) captures frames and decides when to submit (frames.ChangeGate); this
engine owns the network calls (vision.py) and calls `on_event` — from its own thread — with dicts:
  {"type": "started", "frame_id", "stage", "model", "image_size": (w, h)}
  {"type": "mark",    "frame_id", "stage", "mark": Mark}
  {"type": "done",    "frame_id", "stage", "result": StenoResult}
  {"type": "error",   "frame_id", "stage", "message", "budget": bool}
  {"type": "cancelled", "frame_id", "stage"}
The Qt side re-emits them as a queued signal, so widgets are only touched on the GUI thread.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from PIL import Image

from factcheck.pipeline.budget import BudgetExceeded
from factcheck.steno.imaging import prepare_image
from factcheck.steno.vision import StageConfig, VisionBackend

log = logging.getLogger(__name__)
EventCallback = Callable[[dict], None]


@dataclass
class Frame:
    frame_id: int
    image: Image.Image                   # the captured region, physical pixels
    captured_at: float = field(default_factory=time.monotonic)


class LiveEngine:
    def __init__(self, backend: VisionBackend, stages: dict[str, StageConfig], *, on_event: EventCallback,
                 max_image_edge: int = 1568, max_image_tokens: int = 1568, max_inflight_fast: int = 2,
                 hint_provider: Callable[[], str] = lambda: ""):
        self.backend = backend
        self.stages = stages
        self.on_event = on_event
        self.max_image_edge = max_image_edge
        self.max_image_tokens = max_image_tokens
        self.max_inflight_fast = max(1, max_inflight_fast)
        self.hint_provider = hint_provider
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._running: dict[str, dict[asyncio.Task, int]] = {"fast": {}, "deep": {}}   # task -> frame id
        self._pending_fast: Frame | None = None

    # ------------------------------------------------------------------ lifecycle (any thread)
    def start(self) -> None:
        ready = threading.Event()

        def run() -> None:
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            self._loop.call_soon(ready.set)
            self._loop.run_forever()
            pending = asyncio.all_tasks(self._loop)
            for task in pending:
                task.cancel()
            self._loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            self._loop.close()

        self._thread = threading.Thread(target=run, name="steno-engine", daemon=True)
        self._thread.start()
        ready.wait(5)

    def stop(self) -> None:
        if self._loop is not None and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread is not None:
            self._thread.join(timeout=5)

    # ------------------------------------------------------------------ API (any thread)
    def submit(self, frame: Frame, stage: str, *, cancel_deep: bool = False) -> None:
        """Queue an analysis. `cancel_deep=True` when the picture changed (a deep look at it is stale)."""
        assert self._loop is not None, "call start() first"
        self._loop.call_soon_threadsafe(self._submit, frame, stage, cancel_deep)

    def cancel(self, stage: str) -> None:
        assert self._loop is not None, "call start() first"
        self._loop.call_soon_threadsafe(self._cancel_stage, stage)

    def busy(self, stage: str) -> int:
        return len(self._running.get(stage, {}))

    # ------------------------------------------------------------------ loop side
    def _emit(self, event: dict) -> None:
        try:
            self.on_event(event)
        except Exception:
            log.exception("steno event listener failed")

    def _submit(self, frame: Frame, stage: str, cancel_deep: bool) -> None:
        if stage not in self.stages:
            self._emit({"type": "error", "frame_id": frame.frame_id, "stage": stage, "budget": False,
                        "message": f"Stufe '{stage}' ist nicht konfiguriert"})
            return
        if stage == "deep":
            self._cancel_stage("deep")                # only the newest picture deserves the slow look
            self._start(frame, "deep")
            return
        if cancel_deep:
            self._cancel_stage("deep")
        if len(self._running["fast"]) < self.max_inflight_fast:
            self._start(frame, "fast")
        else:
            self._pending_fast = frame                # newest waiting frame wins

    def _cancel_stage(self, stage: str) -> None:
        for task in list(self._running.get(stage, {})):
            task.cancel()
        if stage == "fast":
            self._pending_fast = None

    def _start(self, frame: Frame, stage: str) -> None:
        assert self._loop is not None
        task = self._loop.create_task(self._run(frame, stage))
        self._running[stage][task] = frame.frame_id
        task.add_done_callback(lambda t, s=stage: self._finished(t, s))

    def _finished(self, task: asyncio.Task, stage: str) -> None:
        self._running[stage].pop(task, None)
        if stage == "fast" and self._pending_fast is not None and len(self._running["fast"]) < self.max_inflight_fast:
            frame, self._pending_fast = self._pending_fast, None
            self._start(frame, "fast")

    async def _run(self, frame: Frame, stage: str) -> None:
        cfg = self.stages[stage]
        fid = frame.frame_id
        try:
            prepared = await asyncio.to_thread(prepare_image, frame.image, self.max_image_edge, self.max_image_tokens)
            self._emit({"type": "started", "frame_id": fid, "stage": stage, "model": cfg.model,
                        "image_size": (prepared.width, prepared.height)})
            result = await self.backend.analyze(
                prepared, cfg, frame_id=fid, hint=self.hint_provider(),
                on_mark=lambda m: self._emit({"type": "mark", "frame_id": fid, "stage": stage, "mark": m}))
        except asyncio.CancelledError:
            self._emit({"type": "cancelled", "frame_id": fid, "stage": stage})
            raise
        except BudgetExceeded as exc:
            self._emit({"type": "error", "frame_id": fid, "stage": stage, "budget": True, "message": str(exc)})
            return
        except Exception as exc:                      # never let one frame kill the live loop
            log.exception("steno %s analysis of frame %s crashed", stage, fid)
            self._emit({"type": "error", "frame_id": fid, "stage": stage, "budget": False,
                        "message": f"{type(exc).__name__}: {exc}"[:240]})
            return
        self._emit({"type": "done", "frame_id": fid, "stage": stage, "result": result})
