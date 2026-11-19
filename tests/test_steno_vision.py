"""Steno vision backend against a scripted fake Messages API: request shape, streaming, spend, failures ($0)."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import anthropic
import httpx
import httpx2
import pytest
from PIL import Image

from factcheck.pipeline.budget import BudgetExceeded
from factcheck.steno.imaging import prepare_image
from factcheck.steno.vision import (
    FALLBACK_BETA,
    AnthropicVision,
    GeminiVision,
    MultiVision,
    StageConfig,
    is_gemini_model,
    normalize_gemini_model,
    output_config,
    stages_from_settings,
    thinking_param,
)

DOC = {"items": [
    {"box": [10, 10, 60, 50], "strokes": "Kürzel und", "text": "und", "alternatives": [], "kind": "word",
     "issue": "", "intended": "", "fix": "", "spot": [], "confidence": 0.9},
    {"box": [80, 10, 150, 60], "strokes": "h + au-tief + s", "text": "Hans", "alternatives": ["Hanse"],
     "kind": "error", "issue": "Verstärkung fehlt", "intended": "Haus", "fix": "au: tief + verstärkt",
     "spot": [100, 30], "confidence": 0.5},
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
    stages = stages_from_settings(settings)
    assert set(stages) == {"fast", "deep"}
    assert stages["fast"].model == "claude-sonnet-5-5" and stages["fast"].thinking == "adaptive"
    assert stages["fast"].max_tokens == 4096 and stages["fast"].timeout_s == 45.0
    assert stages["deep"].model == "claude-opus-5-5" and stages["deep"].effort == "high"
    assert stages["deep"].max_tokens == 16000 and stages["deep"].timeout_s == 180.0
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
    deep = vision.build_request(image, DEEP, hint="", context="Vorheriger Satz")
    assert deep["thinking"] == {"type": "adaptive"}
    assert deep["extra_headers"] == {"anthropic-beta": FALLBACK_BETA} and deep["extra_body"] == {"fallbacks": "default"}
    assert deep["system"] == fast["system"]                           # identical prefix for every frame
    assert "Vorheriger Satz" in deep["messages"][0]["content"][1]["text"]


# ------------------------------------------------------------------------------------------------ streaming
async def test_analyze_streams_marks_in_order_and_records_spend(settings, ledger, image):
    text = json.dumps(DOC, ensure_ascii=False)
    vision = make_vision(settings, ledger, FakeStream(chunks_of(text), final_message(text)))
    seen = []
    result = await vision.analyze(image, FAST, frame_id=7, on_mark=seen.append)
    assert [m.text for m in seen] == ["und", "Hans"] == [m.text for m in result.marks]
    assert result.marks[0].strokes == "Kürzel und" and result.marks[1].strokes == "h + au-tief + s"
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


# --------------------------------------------------------------------------------------------------- gemini
def test_is_gemini_model_detection():
    assert is_gemini_model("gemini-2.5-flash")
    assert is_gemini_model("models/gemini-2.5-pro")
    assert is_gemini_model("google/gemini-2.0-flash")
    assert is_gemini_model("neon")
    assert is_gemini_model("argon")
    assert is_gemini_model("carbon")
    assert is_gemini_model("hydrogen")
    assert is_gemini_model("gemini:my-custom-model")
    assert not is_gemini_model("claude-sonnet-5-5")
    assert not is_gemini_model("claude-opus-5-5")
    assert normalize_gemini_model("gemini-2.5-flash") == "models/gemini-2.5-flash"
    assert normalize_gemini_model("gemini:my-model") == "models/my-model"
    assert normalize_gemini_model("models/gemini-1.5-pro") == "models/gemini-1.5-pro"
    assert normalize_gemini_model("neon") == "models/gemini-4-neon"
    assert normalize_gemini_model("argon") == "models/gemini-4-argon"
    assert normalize_gemini_model("carbon") == "models/gemini-4-carbon"
    assert normalize_gemini_model("hydrogen") == "models/gemini-4-hydrogen"
    assert normalize_gemini_model("flash") == "models/gemini-2.5-flash"
    assert normalize_gemini_model("pro") == "models/gemini-2.5-pro"


def test_gemini_request_shape(settings, ledger, image):
    settings.gemini_api_key = "test-gemini-key"
    stage = StageConfig("fast", "gemini-2.5-flash", "low", "off", 4096)
    gv = GeminiVision(settings, ledger)
    body = gv.build_request_body(image, stage, hint="Finde Fehler", context="Vorher")
    assert "system_instruction" in body
    assert "Du bist ein" in body["system_instruction"]["parts"][0]["text"]
    parts = body["contents"][0]["parts"]
    assert parts[0]["inline_data"]["mime_type"] == "image/png"
    assert "Finde Fehler" in parts[1]["text"]
    assert "Vorher" in parts[1]["text"]
    schema = body["generationConfig"]["responseSchema"]
    assert "additionalProperties" not in schema
    assert "additionalProperties" not in schema["properties"]["items"]["items"]
    assert body["generationConfig"]["thinkingConfig"]["thinkingBudget"] == 0

    stage_neon = StageConfig("deep", "neon", "high", "adaptive", 16000)
    body_neon = gv.build_request_body(image, stage_neon, hint="")
    assert body_neon["generationConfig"]["thinkingConfig"]["thinkingLevel"] == "high"


async def test_gemini_streams_marks_and_records_spend(settings, ledger, image):
    settings.gemini_api_key = "test-gemini-key"
    stage = StageConfig("fast", "gemini-2.5-flash", "low", "off", 4096)

    def sse_event(candidate_text: str = "", thought_text: str = "", usage: dict | None = None, finish: str | None = None):
        parts = []
        if thought_text:
            parts.append({"thought": True, "text": thought_text})
        if candidate_text:
            parts.append({"text": candidate_text})
        c: dict = {"content": {"parts": parts}}
        if finish:
            c["finishReason"] = finish
        payload: dict = {"candidates": [c]}
        if usage:
            payload["usageMetadata"] = usage
        return f"data: {json.dumps(payload)}\n\n".encode()

    sse_data = [
        sse_event(thought_text="Ich analysiere jetzt die Zeichen..."),
        sse_event(candidate_text='{"items": [{"box": [10, 10, 60, 50], "strokes": "Kürzel und", '),
        sse_event(candidate_text='"text": "und", "alternatives": [], "kind": "word", "confidence": 0.95}], '),
        sse_event(candidate_text='"answer": "Alles richtig"}',
                  usage={"promptTokenCount": 850, "candidatesTokenCount": 90, "cachedContentTokenCount": 0},
                  finish="STOP"),
    ]

    async def mock_handler(request: httpx.Request):
        assert request.headers.get("x-goog-api-key") == "test-gemini-key"
        assert "models/gemini-2.5-flash" in str(request.url)
        return httpx.Response(200, content=b"".join(sse_data), headers={"content-type": "text/event-stream"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(mock_handler))
    gv = GeminiVision(settings, ledger, http_client=client)
    seen = []
    res = await gv.analyze(image, stage, frame_id=12, on_mark=seen.append)

    assert len(seen) == 1
    assert seen[0].text == "und"
    assert seen[0].strokes == "Kürzel und"
    assert res.answer == "Alles richtig"
    assert res.error is None
    assert res.stop_reason == "STOP"
    assert res.usage["input_tokens"] == 850
    assert res.usage["output_tokens"] == 90
    rows = ledger._rows()
    assert len(rows) == 1
    assert rows[0]["purpose"] == "steno-fast"
    assert rows[0]["model"] == "gemini-2.5-flash"
    assert rows[0]["input_tokens"] == 850
    assert rows[0]["output_tokens"] == 90


async def test_gemini_error_responses(settings, ledger, image):
    settings.gemini_api_key = "test-key"
    stage = StageConfig("fast", "gemini-2.5-flash", "low", "off", 4096)

    async def err_handler_401(request: httpx.Request):
        return httpx.Response(401, json={"error": {"message": "API key not valid"}})

    client_401 = httpx.AsyncClient(transport=httpx.MockTransport(err_handler_401))
    gv_401 = GeminiVision(settings, ledger, http_client=client_401)
    res_401 = await gv_401.analyze(image, stage, frame_id=1)
    assert "ungültig" in res_401.error

    async def err_handler_429(request: httpx.Request):
        return httpx.Response(429, json={"error": {"message": "Resource exhausted"}})

    client_429 = httpx.AsyncClient(transport=httpx.MockTransport(err_handler_429))
    gv_429 = GeminiVision(settings, ledger, http_client=client_429)
    res_429 = await gv_429.analyze(image, stage, frame_id=2)
    assert "Rate-Limit" in res_429.error


async def test_multi_vision_routing(settings, ledger, image):
    fast_claude = StageConfig("fast", "claude-sonnet-5-5", "low", "off", 4096)
    fast_gemini = StageConfig("fast", "gemini-2.5-flash", "low", "off", 4096)
    deep_neon = StageConfig("deep", "neon", "low", "off", 8000)

    class MockBackend:
        def __init__(self, name):
            self.name = name
            self.calls = []

        async def analyze(self, img, stage, *, frame_id, hint="", context="", on_mark=None):
            self.calls.append(stage.model)
            from factcheck.steno.models import StenoResult
            return StenoResult(frame_id=frame_id, stage=stage.stage, model=stage.model,
                               image_width=img.width, image_height=img.height, image_tokens=img.tokens)

    anthropic_mock = MockBackend("anthropic")
    gemini_mock = MockBackend("gemini")
    multi = MultiVision(settings, ledger, anthropic_backend=anthropic_mock, gemini_backend=gemini_mock)

    await multi.analyze(image, fast_claude, frame_id=1)
    assert anthropic_mock.calls == ["claude-sonnet-5-5"]
    assert gemini_mock.calls == []

    await multi.analyze(image, fast_gemini, frame_id=2)
    assert anthropic_mock.calls == ["claude-sonnet-5-5"]
    assert gemini_mock.calls == ["gemini-2.5-flash"]

    await multi.analyze(image, deep_neon, frame_id=3)
    assert gemini_mock.calls == ["gemini-2.5-flash", "neon"]

