"""Steno vision backend against a scripted fake Messages API: request shape, streaming, spend, failures ($0)."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import anthropic
import httpx2
import pytest
from PIL import Image

from factcheck.pipeline.budget import BudgetExceeded
from factcheck.steno.imaging import prepare_image
from factcheck.steno.vision import (
    FALLBACK_BETA,
    AnthropicVision,
    StageConfig,
    output_config,
    stages_from_settings,
    thinking_param,
)

DOC = {"items": [
    {"box": [10, 10, 60, 50], "text": "und", "alternatives": [], "kind": "word", "issue": "", "intended": "",
     "fix": "", "spot": [], "confidence": 0.9},
    {"box": [80, 10, 150, 60], "text": "Hans", "alternatives": ["Hanse"], "kind": "error",
     "issue": "Verstärkung fehlt", "intended": "Haus", "fix": "au: tief + verstärkt", "spot": [100, 30],
     "confidence": 0.5},
], "answer": "Hans statt Haus: Verstärkung fehlt."}
USAGE = {"input_tokens": 900, "output_tokens": 210, "cache_read_input_tokens": 2400, "cache_creation_input_tokens": 0}
FAST = StageConfig("fast", "claude-haiku-5-5", "low", "off", 2048)
DEEP = StageConfig("deep", "claude-opus-5-5", "low", "adaptive", 8000)


class FakeUsage:
    def __init__(self, data):
        self.data = data

    def model_dump(self, exclude_none=True):
        return dict(self.data)


def final_message(text, stop_reason="end_turn", model="claude-haiku-5-5", usage=USAGE, category=None):
    return SimpleNamespace(stop_reason=stop_reason, model=model, usage=FakeUsage(usage),
                           content=[SimpleNamespace(type="text", text=text)],
                           stop_details=SimpleNamespace(category=category) if category else None)


class FakeStream:
    """Async context manager mimicking anthropic's MessageStream (text_stream + get_final_message)."""

    def __init__(self, chunks=(), final=None, enter_error=None, gate: asyncio.Event | None = None):
        self.chunks, self.final, self.enter_error, self.gate = list(chunks), final, enter_error, gate
        self.current_message_snapshot = SimpleNamespace(usage=FakeUsage({"input_tokens": 3000, "output_tokens": 1}))

    async def __aenter__(self):
        if self.enter_error is not None:
            raise self.enter_error
        return self

    async def __aexit__(self, *exc):
        return False

    @property
    def text_stream(self):
        async def gen():
            for i, chunk in enumerate(self.chunks):
                if i == 1 and self.gate is not None:
                    await self.gate.wait()                # block mid-stream (cancellation test)
                await asyncio.sleep(0)
                yield chunk
        return gen()

    async def get_final_message(self):
        return self.final


class FakeClient:
    def __init__(self, *streams):
        self.streams = list(streams)
        self.calls: list[dict] = []
        self.options: list[dict] = []
        self.messages = self

    def with_options(self, **kw):
        self.options.append(kw)
        return self

    def stream(self, **kwargs):
        self.calls.append(kwargs)
        return self.streams.pop(0)


def chunks_of(text, n=7):
    return [text[i:i + n] for i in range(0, len(text), n)]


@pytest.fixture
def image():
    return prepare_image(Image.new("RGB", (200, 80), "white"))


def make_vision(settings, ledger, *streams):
    settings.anthropic_api_key = None
    return AnthropicVision(settings, ledger, client=FakeClient(*streams))


# ------------------------------------------------------------------------------------------ request shape
def test_thinking_and_effort_per_model_family():
    assert thinking_param("claude-haiku-5-5", "off", "low") == {"type": "disabled"}
    assert thinking_param("claude-haiku-5-5", "off", "xhigh") == {"type": "adaptive"}   # off is a 400 there
    assert thinking_param("claude-sonnet-5-5", "off", "low") == {"type": "between_tools"}
    assert thinking_param("claude-opus-5-5", "off", "low") is None                      # cannot be disabled
    assert thinking_param("claude-opus-5-5", "adaptive", "low") == {"type": "adaptive"}
    assert output_config("claude-haiku-5-5", "low")["effort"] == "low"
    assert "effort" not in output_config("claude-haiku-4-5", "low")                     # effort errors there


def test_stages_from_settings(settings):
    assert set(stages_from_settings(settings)) == {"fast", "deep"}
    settings.steno_deep_model = ""
    assert set(stages_from_settings(settings)) == {"fast"}


def test_build_request_is_cache_friendly_and_well_formed(settings, ledger, image):
    vision = make_vision(settings, ledger)
    fast = vision.build_request(image, FAST, hint="Finde den Fehler")
    assert fast["model"] == "claude-haiku-5-5" and fast["thinking"] == {"type": "disabled"}
    assert fast["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert fast["output_config"]["format"]["type"] == "json_schema" and fast["output_config"]["effort"] == "low"
    img_block, text_block = fast["messages"][0]["content"]
    assert img_block["type"] == "image" and img_block["source"]["media_type"] == "image/png"
    assert text_block["text"].startswith(f"Bildgröße: {image.width}×{image.height}") and "Finde den Fehler" in text_block["text"]
    assert "extra_body" not in fast                                   # Haiku 5.5 has no server-side fallback
    deep = vision.build_request(image, DEEP, hint="")
    assert deep["thinking"] == {"type": "adaptive"}
    assert deep["extra_headers"] == {"anthropic-beta": FALLBACK_BETA} and deep["extra_body"] == {"fallbacks": "default"}
    assert deep["system"] == fast["system"]                           # identical prefix for every frame


# ------------------------------------------------------------------------------------------------ streaming
async def test_analyze_streams_marks_in_order_and_records_spend(settings, ledger, image):
    text = json.dumps(DOC, ensure_ascii=False)
    vision = make_vision(settings, ledger, FakeStream(chunks_of(text), final_message(text)))
    seen = []
    result = await vision.analyze(image, FAST, frame_id=7, on_mark=seen.append)
    assert [m.text for m in seen] == ["und", "Hans"] == [m.text for m in result.marks]
    assert result.answer == DOC["answer"] and result.error is None and result.stop_reason == "end_turn"
    assert result.ttft_s is not None and result.first_mark_s is not None
    assert result.errors[0].intended == "Haus" and result.errors[0].spot == (100.0, 30.0)
    rows = ledger._rows()
    assert len(rows) == 1 and rows[0]["purpose"] == "steno-fast" and rows[0]["frame_id"] == 7
    assert result.cost_usd == pytest.approx(rows[0]["cost_usd"]) and result.cost_usd > 0
    assert vision.client.options[0] == {"max_retries": 0, "timeout": 30.0}
    assert ledger.remaining() == pytest.approx(10.0 - result.cost_usd)    # reservation released


async def test_refusal_drops_partial_marks(settings, ledger, image):
    text = json.dumps(DOC)
    vision = make_vision(settings, ledger, FakeStream(chunks_of(text), final_message(text, "refusal", category="cyber")))
    result = await vision.analyze(image, FAST, frame_id=1)
    assert result.marks == [] and "abgelehnt" in result.error and "cyber" in result.error


async def test_max_tokens_keeps_complete_items(settings, ledger, image):
    text = json.dumps(DOC)
    cut = text[: text.index('"answer"')]
    vision = make_vision(settings, ledger, FakeStream(chunks_of(cut), final_message(cut, "max_tokens")))
    result = await vision.analyze(image, FAST, frame_id=1)
    assert len(result.marks) == 2 and "abgeschnitten" in result.error


async def test_connection_error_is_reported_without_spend(settings, ledger, image):
    err = anthropic.APIConnectionError(request=httpx2.Request("POST", "https://api.anthropic.com/v1/messages"))
    vision = make_vision(settings, ledger, FakeStream(enter_error=err))
    result = await vision.analyze(image, FAST, frame_id=1)
    assert result.error == "Keine Verbindung zur API" and ledger._rows() == []
    assert ledger.remaining() == pytest.approx(10.0)


async def test_missing_credentials_message(settings, ledger, image):
    err = TypeError("Could not resolve authentication method. Expected one of api_key, auth_token, ...")
    vision = make_vision(settings, ledger, FakeStream(enter_error=err))
    result = await vision.analyze(image, FAST, frame_id=1)
    assert "Zugangsdaten" in result.error


async def test_rejected_fallback_beta_is_dropped_for_the_session(settings, ledger, image):
    text = json.dumps(DOC)
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    bad = anthropic.BadRequestError("fallbacks: unsupported beta", response=httpx2.Response(400, request=request),
                                    body=None)
    vision = make_vision(settings, ledger, FakeStream(enter_error=bad),
                         FakeStream(chunks_of(text), final_message(text, model="claude-opus-5-5")))
    result = await vision.analyze(image, DEEP, frame_id=3)
    assert result.error is None and len(result.marks) == 2
    first, second = vision.client.calls
    assert "extra_body" in first and "extra_body" not in second and "extra_headers" not in second
    assert "extra_body" not in vision.build_request(image, DEEP, "")


async def test_cancel_mid_stream_charges_partial_usage(settings, ledger, image):
    text = json.dumps(DOC)
    gate = asyncio.Event()                                            # never set: the stream hangs after chunk 1
    vision = make_vision(settings, ledger, FakeStream([text[:10], text[10:]], final_message(text), gate=gate))
    task = asyncio.create_task(vision.analyze(image, DEEP, frame_id=9))
    for _ in range(50):
        await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    rows = ledger._rows()
    assert len(rows) == 1 and rows[0]["partial"] is True and rows[0]["cancelled"] is True
    assert rows[0]["purpose"] == "steno-deep" and rows[0]["input_tokens"] == 3000
    assert ledger.remaining() == pytest.approx(10.0 - rows[0]["cost_usd"])


# -------------------------------------------------------------------------------------------------- budget
async def test_steno_daily_cap_blocks_before_calling(settings, ledger, image):
    settings.steno_daily_budget_usd = 0.00001
    vision = make_vision(settings, ledger)
    with pytest.raises(BudgetExceeded):
        await vision.analyze(image, FAST, frame_id=1)
    assert vision.client.calls == []


async def test_deep_share_stops_deep_before_fast(settings, ledger, image):
    settings.steno_daily_budget_usd, settings.steno_deep_budget_share = 1.0, 0.5
    ledger.record(purpose="steno-fast", model="claude-haiku-5-5", auth="api_key",
                  usage={"server_tool_use": {"web_search_requests": 55}}, sdk_cost_usd=None, run_id="x")   # $0.55
    text = json.dumps(DOC)
    vision = make_vision(settings, ledger, FakeStream(chunks_of(text), final_message(text)))
    with pytest.raises(BudgetExceeded, match="Deep"):
        await vision.analyze(image, DEEP, frame_id=1)
    assert (await vision.analyze(image, FAST, frame_id=2)).error is None
