"""LiveEngine scheduling on its background loop: events, newest-wins, deep cancellation, budget errors ($0)."""

from __future__ import annotations

import asyncio
import threading
import time

from PIL import Image, ImageDraw

from factcheck.pipeline.budget import BudgetExceeded, BudgetLedger
from factcheck.steno.engine import Frame, LiveEngine
from factcheck.steno.models import Mark, StenoResult
from factcheck.steno.vision import FakeVision, StageConfig

STAGES = {"fast": StageConfig("fast", "claude-haiku-5-5"), "deep": StageConfig("deep", "claude-opus-5-5")}


def picture() -> Image.Image:
    img = Image.new("RGB", (240, 90), "white")
    d = ImageDraw.Draw(img)
    d.line([(20, 20), (40, 70)], fill="black", width=4)
    d.line([(120, 20), (160, 60)], fill="black", width=4)
    return img


class Recorder:
    def __init__(self):
        self.events: list[dict] = []
        self.lock = threading.Lock()

    def __call__(self, ev: dict) -> None:
        with self.lock:
            self.events.append(ev)

    def kinds(self, kind: str) -> list[dict]:
        with self.lock:
            return [e for e in self.events if e["type"] == kind]

    def wait(self, kind: str, n: int = 1, timeout: float = 5.0) -> list[dict]:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            got = self.kinds(kind)
            if len(got) >= n:
                return got
            time.sleep(0.01)
        raise AssertionError(f"waited for {n} {kind!r} events, got {self.events}")


class ScriptedBackend:
    """Fast stage sleeps `fast_s`; deep stage waits forever (until cancelled); can raise BudgetExceeded."""

    auth = "fake"

    def __init__(self, fast_s: float = 0.0, budget_error: bool = False):
        self.fast_s, self.budget_error = fast_s, budget_error
        self.frames: list[tuple[str, int]] = []

    async def analyze(self, image, stage, *, frame_id, hint="", on_mark=None):
        self.frames.append((stage.stage, frame_id))
        if self.budget_error:
            raise BudgetExceeded("Steno-Tagesbudget erreicht")
        if stage.stage == "deep":
            await asyncio.Event().wait()
        await asyncio.sleep(self.fast_s)
        if on_mark:
            on_mark(Mark(kind="word", box=(1, 1, 9, 9), text=f"f{frame_id}"))
        return StenoResult(frame_id=frame_id, stage=stage.stage, model=stage.model)


def engine_with(backend, rec, **kw) -> LiveEngine:
    eng = LiveEngine(backend, STAGES, on_event=rec, **kw)
    eng.start()
    return eng


def test_events_flow_from_started_to_done_with_fake_vision():
    rec = Recorder()
    eng = engine_with(FakeVision(delay_s=0.01), rec, hint_provider=lambda: "Finde den Fehler")
    try:
        eng.submit(Frame(1, picture()), "fast")
        done = rec.wait("done")[0]
        started = rec.kinds("started")[0]
        assert started["image_size"] == (240, 90) and started["model"] == "claude-haiku-5-5"
        marks = rec.kinds("mark")
        assert len(marks) == len(done["result"].marks) == 2
        assert all(e["frame_id"] == 1 and e["stage"] == "fast" for e in marks)
    finally:
        eng.stop()


def test_new_content_cancels_a_running_deep_look():
    rec = Recorder()
    eng = engine_with(ScriptedBackend(), rec)
    try:
        eng.submit(Frame(1, picture()), "deep")
        rec.wait("started")
        eng.submit(Frame(2, picture()), "fast", cancel_deep=True)
        cancelled = rec.wait("cancelled")[0]
        assert cancelled["stage"] == "deep" and cancelled["frame_id"] == 1
        assert rec.wait("done")[0]["frame_id"] == 2
    finally:
        eng.stop()


def test_waiting_fast_frame_is_replaced_by_newer_one():
    rec = Recorder()
    backend = ScriptedBackend(fast_s=0.3)
    eng = engine_with(backend, rec, max_inflight_fast=1)
    try:
        for fid in (1, 2, 3):
            eng.submit(Frame(fid, picture()), "fast")
        done = rec.wait("done", n=2)
        time.sleep(0.4)
        assert [e["frame_id"] for e in done] == [1, 3]                  # frame 2 was superseded while waiting
        assert backend.frames == [("fast", 1), ("fast", 3)]
    finally:
        eng.stop()


def test_budget_errors_are_flagged(tmp_path):
    rec = Recorder()
    eng = engine_with(ScriptedBackend(budget_error=True), rec)
    try:
        eng.submit(Frame(1, picture()), "fast")
        err = rec.wait("error")[0]
        assert err["budget"] is True and "Tagesbudget" in err["message"]
    finally:
        eng.stop()


def test_unknown_stage_reports_error():
    rec = Recorder()
    eng = LiveEngine(ScriptedBackend(), {"fast": STAGES["fast"]}, on_event=rec)
    eng.start()
    try:
        eng.submit(Frame(1, picture()), "deep")
        assert "nicht konfiguriert" in rec.wait("error")[0]["message"]
    finally:
        eng.stop()


def test_ledger_reads_rows_appended_by_other_processes(tmp_path):
    path = tmp_path / "ledger.jsonl"
    ledger = BudgetLedger(path, total_cap_usd=10.0)
    ledger.record(purpose="steno-fast", model="claude-haiku-5-5", auth="api_key", usage=None, sdk_cost_usd=0.5,
                  run_id="a")
    assert ledger.spent_total() == 0.5
    today = time.strftime("%Y-%m-%d")
    with path.open("a", encoding="utf-8") as fh:                   # another process appends; last line unfinished
        fh.write(f'{{"ts": "{today}T10:00:00", "purpose": "app-verify", "cost_usd": 0.25}}\n')
        fh.write(f'{{"ts": "{today}T10:00:01", "purpose": "steno-deep", "cost_usd": 1.0}}')
    assert ledger.spent_total() == 1.75
    assert ledger.spent_today("steno") == 1.5 and ledger.spent_today_app() == 0.25
    with path.open("a", encoding="utf-8") as fh:
        fh.write("\n")
    assert ledger.spent_total() == 1.75                             # the finished line is not counted twice
    path.write_text("", encoding="utf-8")                           # file replaced: start over
    assert ledger.spent_total() == 0.0
