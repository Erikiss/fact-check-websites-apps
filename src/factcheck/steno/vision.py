"""Vision backends for the live steno mode: the Messages API (streaming) and a $0 offline fake.

Local context: `StageConfig` (model, effort, thinking per stage) and how it maps onto request parameters per
model family; `AnthropicVision.analyze()` streams one structured-output answer and hands over each mark as soon
as its JSON object is complete; `FakeVision` finds ink blobs and invents labels so the UI runs without a key.
Global context: engine.py calls `analyze()` for every triggered frame. Spend goes through the project's shared
BudgetLedger (purposes "steno-fast" / "steno-deep") plus a steno-only daily cap, so a live loop cannot run away.
API facts used (claude-api skill, checked 2026-11-08):
  * Haiku 5.5 takes thinking {"type": "disabled"} at effort <= high; Sonnet 5.5 rejects "disabled" and turns
    thinking off with {"type": "between_tools"}; Opus 5.5 cannot turn it off (effort is the only lever).
  * Structured outputs: output_config.format (json_schema); effort lives in output_config too.
  * Prompt caching: cache_control on the static system prompt (512-token minimum on the 5.x models).
  * Refusal fallback: `fallbacks: "default"` + beta header server-side-fallback-2026-07-01 (not for Haiku 5.5).
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

import anthropic
import numpy as np
from PIL import Image

from factcheck.config import Settings
from factcheck.log import llm_event
from factcheck.pipeline.budget import PRICES_PER_MTOK, BudgetExceeded, BudgetLedger
from factcheck.steno.imaging import PreparedImage
from factcheck.steno.jsonstream import ItemStream
from factcheck.steno.models import ANALYSIS_SCHEMA, Mark, Stage, StenoResult
from factcheck.steno.prompts import build_system_prompt, build_user_text, load_knowledge

log = logging.getLogger(__name__)

FALLBACK_BETA = "server-side-fallback-2026-07-01"
_FALLBACK_MODELS = ("claude-opus-5", "claude-sonnet-5-5", "claude-fable-5")   # prefixes; Haiku 5.5 has none
_THINKING_ALWAYS_ON = ("claude-opus-5-5", "claude-fable", "claude-mythos")
_BETWEEN_TOOLS = ("claude-sonnet-5-5",)
_NO_EFFORT = ("claude-haiku-4-5", "claude-sonnet-4-5", "claude-3")
MarkCallback = Callable[[Mark], None]


@dataclass(frozen=True)
class StageConfig:
    stage: Stage
    model: str
    effort: str = "low"
    thinking: Literal["off", "adaptive"] = "off"
    max_tokens: int = 2048
    fallbacks: bool = True
    max_retries: int = 0          # a retried live frame is stale anyway: the next frame is the retry
    timeout_s: float = 30.0


def stages_from_settings(s: Settings) -> dict[str, StageConfig]:
    """The fast stage always; the deep stage only if STENO_DEEP_MODEL is set."""
    stages = {"fast": StageConfig("fast", s.steno_fast_model, s.steno_fast_effort, s.steno_fast_thinking,
                                  s.steno_fast_max_tokens, s.steno_fallbacks, max_retries=0, timeout_s=30.0)}
    if s.steno_deep_model.strip():
        stages["deep"] = StageConfig("deep", s.steno_deep_model.strip(), s.steno_deep_effort, s.steno_deep_thinking,
                                     s.steno_deep_max_tokens, s.steno_fallbacks, max_retries=1, timeout_s=120.0)
    return stages


def thinking_param(model: str, mode: str, effort: str) -> dict[str, Any] | None:
    """The `thinking` request value for this model, or None to leave it out."""
    if model.startswith(_THINKING_ALWAYS_ON):
        return {"type": "adaptive"} if mode == "adaptive" else None   # cannot be disabled; low effort keeps it short
    if mode == "adaptive" or effort in ("xhigh", "max"):            # "off" is rejected at xhigh/max
        return {"type": "adaptive"}
    if model.startswith(_BETWEEN_TOOLS):
        return {"type": "between_tools"}                            # no tools in this request = no thinking
    return {"type": "disabled"}


def output_config(model: str, effort: str) -> dict[str, Any]:
    cfg: dict[str, Any] = {"format": {"type": "json_schema", "schema": ANALYSIS_SCHEMA}}
    if not model.startswith(_NO_EFFORT):
        cfg["effort"] = effort
    return cfg


def supports_fallbacks(model: str) -> bool:
    return model.startswith(_FALLBACK_MODELS)


def worst_case_usd(model: str, input_tokens: int, max_tokens: int) -> float:
    """Upper bound for one call: every input token as a cache write, the full max_tokens as output."""
    p = PRICES_PER_MTOK.get(model) or max(PRICES_PER_MTOK.values(), key=lambda row: row["output"])
    return round((input_tokens * max(p["input"], p["cache_write_5m"]) + max_tokens * p["output"]) / 1e6, 6)


def describe_error(exc: BaseException) -> str:
    """Short German message for the status line (most specific SDK error first)."""
    if isinstance(exc, anthropic.AuthenticationError):
        return "API-Schlüssel ungültig (ANTHROPIC_API_KEY in .env prüfen)"
    if isinstance(exc, anthropic.PermissionDeniedError):
        return "Kein Zugriff auf dieses Modell mit diesem Schlüssel"
    if isinstance(exc, anthropic.NotFoundError):
        return "Modell nicht gefunden (STENO_*_MODEL prüfen)"
    if isinstance(exc, anthropic.RateLimitError):
        return "Rate-Limit erreicht – der nächste Frame versucht es erneut"
    if isinstance(exc, anthropic.BadRequestError):
        return f"Anfrage abgelehnt: {getattr(exc, 'message', exc)}"[:240]
    if isinstance(exc, anthropic.APIStatusError):
        return f"API-Fehler {exc.status_code}: {getattr(exc, 'message', exc)}"[:240]
    if isinstance(exc, anthropic.APITimeoutError):
        return "Zeitüberschreitung bei der Anfrage"
    if isinstance(exc, anthropic.APIConnectionError):
        return "Keine Verbindung zur API"
    if isinstance(exc, TypeError) and "authentication" in str(exc).lower():
        return "Keine Zugangsdaten: ANTHROPIC_API_KEY in .env eintragen (oder STENO_FAKE=true zum Testen)"
    return f"{type(exc).__name__}: {exc}"[:240]


class VisionBackend(Protocol):
    auth: str

    async def analyze(self, image: PreparedImage, stage: StageConfig, *, frame_id: int, hint: str = "",
                      on_mark: MarkCallback | None = None) -> StenoResult: ...


class StenoBudget:
    """Steno's own daily cap, on top of the project-wide cap that BudgetLedger.reserve() enforces."""

    def __init__(self, ledger: BudgetLedger, daily_usd: float, deep_share: float):
        self.ledger = ledger
        self.daily_usd = daily_usd
        self.deep_share = deep_share

    def spent_today(self) -> float:
        return self.ledger.spent_today("steno")

    def check(self, stage: str, worst_case: float) -> None:
        cap = self.daily_usd * (self.deep_share if stage == "deep" else 1.0)
        spent = self.spent_today()
        if spent + worst_case > cap:
            which = "Deep-Anteil des Steno-Tagesbudgets" if stage == "deep" else "Steno-Tagesbudget"
            raise BudgetExceeded(f"{which} (${cap:.2f}) erreicht – heute ${spent:.4f} ausgegeben "
                                 f"(STENO_DAILY_BUDGET_USD in .env)")


def _last_json_block(message: Any) -> dict[str, Any] | None:
    """The answer document from the final message (fallback when the streamed text did not parse)."""
    for block in reversed(list(getattr(message, "content", None) or [])):
        if getattr(block, "type", "") == "text":
            try:
                value = json.loads(block.text)
            except ValueError:
                continue
            if isinstance(value, dict):
                return value
    return None


class AnthropicVision:
    """One streamed Messages API call per analysed frame."""

    def __init__(self, settings: Settings, ledger: BudgetLedger, client: Any | None = None,
                 system_prompt: str | None = None):
        self.settings = settings
        self.ledger = ledger
        self.budget = StenoBudget(ledger, settings.steno_daily_budget_usd, settings.steno_deep_budget_share)
        key = settings.anthropic_api_key.get_secret_value() if settings.anthropic_api_key else None
        if client is None:
            # Without a key in .env the SDK falls back to ANTHROPIC_API_KEY / an `ant auth login` profile.
            client = anthropic.AsyncAnthropic(api_key=key) if key else anthropic.AsyncAnthropic()
        self.client = client
        self.auth = "api_key" if key else "sdk-default"
        self.system_prompt = system_prompt or build_system_prompt(load_knowledge(settings.steno_knowledge_file or None))
        self._system_tokens = len(self.system_prompt) // 3 + 50    # generous, for worst-case reservations only
        self._frames_dir: Path | None = settings.logs_dir / "steno-frames" if settings.steno_save_frames else None
        self._fallbacks_ok = True                  # switched off for the session if the API rejects the beta
        llm_event("steno_system_prompt", "-", text=self.system_prompt)

    def build_request(self, image: PreparedImage, stage: StageConfig, hint: str) -> dict[str, Any]:
        """Keyword arguments for `client.messages.stream(...)`."""
        content = [
            {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                         "data": base64.standard_b64encode(image.png).decode("ascii")}},
            {"type": "text", "text": build_user_text(image.width, image.height, hint)},
        ]
        kwargs: dict[str, Any] = {
            "model": stage.model,
            "max_tokens": stage.max_tokens,
            "system": [{"type": "text", "text": self.system_prompt, "cache_control": {"type": "ephemeral"}}],
            "messages": [{"role": "user", "content": content}],
            "output_config": output_config(stage.model, stage.effort),
        }
        thinking = thinking_param(stage.model, stage.thinking, stage.effort)
        if thinking is not None:
            kwargs["thinking"] = thinking
        if stage.fallbacks and self._fallbacks_ok and supports_fallbacks(stage.model):
            kwargs["extra_headers"] = {"anthropic-beta": FALLBACK_BETA}
            kwargs["extra_body"] = {"fallbacks": "default"}
        return kwargs

    def _save_frame(self, run_id: str, image: PreparedImage) -> str | None:
        if self._frames_dir is None:
            return None
        self._frames_dir.mkdir(parents=True, exist_ok=True)
        path = self._frames_dir / f"{run_id}.png"
        path.write_bytes(image.png)
        return str(path)

    async def analyze(self, image: PreparedImage, stage: StageConfig, *, frame_id: int, hint: str = "",
                      on_mark: MarkCallback | None = None) -> StenoResult:
        """Stream one analysis; raises BudgetExceeded before spending anything if a cap would be broken."""
        worst = worst_case_usd(stage.model, self._system_tokens + image.tokens + 200, stage.max_tokens)
        self.budget.check(stage.stage, worst)
        purpose = f"steno-{stage.stage}"
        self.ledger.reserve(worst, purpose)
        run_id = uuid.uuid4().hex[:10]
        result = StenoResult(frame_id=frame_id, stage=stage.stage, model=stage.model, image_width=image.width,
                             image_height=image.height, image_tokens=image.tokens)
        kwargs = self.build_request(image, stage, hint)
        llm_event("steno_request", run_id, stage=stage.stage, model=stage.model, frame_id=frame_id, hint=hint,
                  image={"width": image.width, "height": image.height, "tokens": image.tokens, "sha1": image.sha1,
                         "path": self._save_frame(run_id, image)},
                  thinking=kwargs.get("thinking"), effort=kwargs["output_config"].get("effort"),
                  fallbacks="extra_body" in kwargs)
        parser = ItemStream()
        holder: dict[str, Any] = {}                       # the open stream, for usage if we are cancelled
        final = None
        t0 = time.monotonic()
        try:
            while True:
                try:
                    final = await self._stream(kwargs, stage, image, parser, result, on_mark, t0, holder)
                    break
                except anthropic.BadRequestError as exc:
                    if "extra_body" in kwargs and "fallback" in str(exc).lower() and not parser.text:
                        log.warning("server-side refusal fallback not accepted (%s); continuing without it", exc)
                        self._fallbacks_ok = False
                        kwargs.pop("extra_body")
                        kwargs.pop("extra_headers")
                        continue
                    raise
        except asyncio.CancelledError:                     # newer picture or user stop: charge what was used
            result.cancelled = True
            result.duration_s = round(time.monotonic() - t0, 3)
            self._record_partial(run_id, purpose, stage, image, holder.get("stream"), parser, result)
            raise
        except anthropic.APIError as exc:                  # status, connection and timeout errors
            result.error = describe_error(exc)
            log.warning("steno %s analysis failed: %s", stage.stage, result.error)
        except TypeError as exc:                           # the SDK's "no credentials" error; anything else is a bug
            if "authentication" not in str(exc).lower():
                raise
            result.error = describe_error(exc)
            log.warning("steno %s analysis failed: %s", stage.stage, result.error)
        finally:
            self.ledger.release(worst)
        result.duration_s = round(time.monotonic() - t0, 3)
        if final is not None:
            self._finish(result, final, parser, image)
            result.cost_usd = self.ledger.record(
                purpose=purpose, model=stage.model, auth=self.auth, usage=result.usage, sdk_cost_usd=None,
                run_id=run_id, extra={"frame_id": frame_id, "served_by": result.served_by,
                                      "image_tokens": image.tokens, "ttft_s": result.ttft_s,
                                      "duration_s": result.duration_s, "stop_reason": result.stop_reason})
        elif parser.text:                                  # failed mid-stream: tokens were still used
            self._record_partial(run_id, purpose, stage, image, holder.get("stream"), parser, result)
        llm_event("steno_result", run_id, stage=stage.stage, frame_id=frame_id, output=parser.text,
                  stop_reason=result.stop_reason, usage=result.usage, cost_usd=result.cost_usd,
                  ttft_s=result.ttft_s, first_mark_s=result.first_mark_s, duration_s=result.duration_s,
                  served_by=result.served_by, error=result.error)
        return result

    async def _stream(self, kwargs: dict[str, Any], stage: StageConfig, image: PreparedImage, parser: ItemStream,
                      result: StenoResult, on_mark: MarkCallback | None, t0: float, holder: dict[str, Any]) -> Any:
        """One streamed request: feed text into the parser, hand out marks, return the final message."""
        client = self.client.with_options(max_retries=stage.max_retries, timeout=stage.timeout_s)
        async with client.messages.stream(**kwargs) as stream:
            holder["stream"] = stream
            async for text in stream.text_stream:
                if result.ttft_s is None:
                    result.ttft_s = round(time.monotonic() - t0, 3)
                for raw in parser.feed(text):
                    mark = Mark.from_raw(raw, image.width, image.height)
                    if mark is None:
                        continue
                    if result.first_mark_s is None:
                        result.first_mark_s = round(time.monotonic() - t0, 3)
                    result.marks.append(mark)
                    if on_mark is not None:
                        on_mark(mark)
            return await stream.get_final_message()

    def _finish(self, result: StenoResult, final: Any, parser: ItemStream, image: PreparedImage) -> None:
        result.stop_reason = str(getattr(final, "stop_reason", "") or "")
        result.served_by = str(getattr(final, "model", "") or "")
        usage = getattr(final, "usage", None)
        result.usage = usage.model_dump(exclude_none=True) if usage is not None else {}
        if result.stop_reason == "refusal":
            details = getattr(final, "stop_details", None)
            category = getattr(details, "category", None) if details is not None else None
            result.error = f"Modell hat die Analyse abgelehnt ({category or 'ohne Kategorie'})"
            result.marks = []                              # partial output before a refusal is not trustworthy
            return
        data = parser.final() or _last_json_block(final)
        if data is not None:
            result.answer = str(data.get("answer") or "").strip()
            if not result.marks:                           # nothing parsed while streaming: use the final document
                for raw in data.get("items") or []:
                    mark = Mark.from_raw(raw, image.width, image.height) if isinstance(raw, dict) else None
                    if mark is not None:
                        result.marks.append(mark)
        if result.stop_reason == "max_tokens":
            result.error = "Antwort abgeschnitten (STENO_*_MAX_TOKENS erhöhen)"

    def _record_partial(self, run_id: str, purpose: str, stage: StageConfig, image: PreparedImage, stream: Any,
                        parser: ItemStream, result: StenoResult) -> None:
        """Ledger row for a call that never finished: what the stream reported, or a conservative estimate."""
        usage: dict[str, Any] = {}
        try:
            snapshot = stream.current_message_snapshot if stream is not None else None
            if snapshot is not None and snapshot.usage is not None:
                usage = snapshot.usage.model_dump(exclude_none=True)
        except (AssertionError, AttributeError, RuntimeError):   # no message_start yet
            usage = {}
        usage.setdefault("input_tokens", self._system_tokens + image.tokens)   # assume no cache hit
        usage["output_tokens"] = max(int(usage.get("output_tokens") or 0), len(parser.text) // 3)
        result.usage = usage
        result.cost_usd = self.ledger.record(purpose=purpose, model=stage.model, auth=self.auth, usage=usage,
                                             sdk_cost_usd=None, run_id=run_id,
                                             extra={"frame_id": result.frame_id, "partial": True,
                                                    "cancelled": result.cancelled})


# ----------------------------------------------------------------------------------------------------- fake
def _runs(mask: np.ndarray, max_gap: int) -> list[tuple[int, int]]:
    """Index ranges where `mask` is true, merging gaps of at most `max_gap`."""
    idx = np.flatnonzero(mask)
    if idx.size == 0:
        return []
    runs, start, prev = [], int(idx[0]), int(idx[0])
    for v in idx[1:]:
        v = int(v)
        if v - prev > max_gap + 1:
            runs.append((start, prev))
            start = v
        prev = v
    runs.append((start, prev))
    return runs


def find_ink_boxes(gray: np.ndarray, min_size: int = 4) -> list[tuple[int, int, int, int]]:
    """Rough word boxes: rows with ink form lines, column gaps inside a line separate words."""
    h, _ = gray.shape
    background = float(np.median(gray))
    ink = np.abs(gray.astype(np.int16) - background) > 60
    boxes = []
    for y0, y1 in _runs(ink.any(axis=1), max_gap=max(2, h // 60)):
        band = ink[y0:y1 + 1]
        for x0, x1 in _runs(band.any(axis=0), max_gap=max(6, (y1 - y0 + 1) // 2)):
            rows = np.flatnonzero(band[:, x0:x1 + 1].any(axis=1))
            if x1 - x0 + 1 >= min_size and rows.size:
                boxes.append((x0, y0 + int(rows[0]), x1 + 1, y0 + int(rows[-1]) + 1))
    return boxes


class FakeVision:
    """Deterministic $0 stand-in (STENO_FAKE=true): labels ink blobs, streaming them with small delays."""

    auth = "fake"

    def __init__(self, delay_s: float = 0.15):
        self.delay_s = delay_s

    async def analyze(self, image: PreparedImage, stage: StageConfig, *, frame_id: int, hint: str = "",
                      on_mark: MarkCallback | None = None) -> StenoResult:
        t0 = time.monotonic()
        gray = np.asarray(Image.open(io.BytesIO(image.png)).convert("L"))
        boxes = find_ink_boxes(gray)[:12]
        result = StenoResult(frame_id=frame_id, stage=stage.stage, model=f"fake:{stage.model}", served_by="fake",
                             image_width=image.width, image_height=image.height, image_tokens=image.tokens)
        bad = len(boxes) // 2 if boxes else -1
        for i, (x1, y1, x2, y2) in enumerate(boxes):
            await asyncio.sleep(self.delay_s * (2 if stage.stage == "deep" else 1))
            if i == bad:
                mark = Mark(kind="error", box=(x1, y1, x2, y2), text=f"Wort {i + 1}", alternatives=[f"Wort {i + 1}b"],
                            issue="FAKE: Verbindung zu rund statt spitz?", intended=f"Wort {i + 1} (richtig)",
                            fix="FAKE: spitz anschließen", spot=((x1 + x2) / 2, (y1 + y2) / 2), confidence=0.45)
            else:
                mark = Mark(kind="word", box=(x1, y1, x2, y2), text=f"Wort {i + 1}",
                            alternatives=[f"Wort {i + 1}?"] if i % 3 == 1 else [], confidence=0.8)
            if result.ttft_s is None:
                result.ttft_s = result.first_mark_s = round(time.monotonic() - t0, 3)
            result.marks.append(mark)
            if on_mark is not None:
                on_mark(mark)
        result.answer = (f"FAKE-Modus (keine echte Analyse): verdächtig ist Wort {bad + 1}." if boxes
                         else "FAKE-Modus: keine Schrift gefunden.")
        result.stop_reason = "end_turn"
        result.duration_s = round(time.monotonic() - t0, 3)
        return result
