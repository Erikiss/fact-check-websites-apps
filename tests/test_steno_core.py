"""Steno live mode, Qt-free building blocks: resizing, streamed JSON, change gate, marks, prompts, layout ($0)."""

from __future__ import annotations

import io
import json
import random

import numpy as np
import pytest
from PIL import Image

from factcheck.steno.frames import ChangeGate, GateConfig, Trigger, changed_fraction, signature
from factcheck.steno.imaging import HIGH_RES_TIER, count_image_tokens, prepare_image, resized_size
from factcheck.steno.jsonstream import ItemStream
from factcheck.steno.layout import Rect, label_lines, place_labels
from factcheck.steno.models import ANALYSIS_SCHEMA, Mark, StenoResult
from factcheck.steno.prompts import build_system_prompt, build_user_text, load_knowledge
from factcheck.steno.render import format_mark, render_annotated
from factcheck.steno.ui.winapi import MOD_ALT, MOD_CONTROL, MOD_SHIFT, parse_hotkey, qt_sequence


# ------------------------------------------------------------------------------------------------ imaging
def test_resized_size_matches_vision_docs_examples():
    assert resized_size(1075, 1520) == (924, 1307)          # A4 scan example (standard tier)
    assert resized_size(1920, 1080) == (1456, 819)          # screenshot example (standard tier)
    assert resized_size(1920, 1080, *HIGH_RES_TIER) == (1920, 1080)
    assert resized_size(1000, 1000) == (1000, 1000)          # fits: unchanged
    assert count_image_tokens(1000, 1000) == 1296


def test_prepare_image_fits_limits_and_maps_back():
    big = Image.new("RGB", (3000, 1000), "white")
    prep = prepare_image(big, max_edge=1568, max_tokens=1568)
    assert prep.width <= 1568 and count_image_tokens(prep.width, prep.height) <= 1568
    decoded = Image.open(io.BytesIO(prep.png))
    assert decoded.size == (prep.width, prep.height) and decoded.format == "PNG"
    sx, sy = prep.scale_to_source()
    assert sx == pytest.approx(3000 / prep.width) and sy == pytest.approx(1000 / prep.height)


# ------------------------------------------------------------------------------------------- json stream
DOC = {"items": [
    {"box": [1, 2, 30, 40], "text": "Haus {mit} \"Klammern\" ]", "alternatives": ["Hans"], "kind": "error",
     "issue": "Verstärkung fehlt", "intended": "Haus", "fix": "a verstärken", "spot": [5, 6], "confidence": 0.4},
    {"box": [40, 2, 90, 40], "text": "und", "alternatives": [], "kind": "word", "issue": "", "intended": "",
     "fix": "", "spot": [], "confidence": 0.9},
], "answer": "Im ersten Wort fehlt die Verstärkung."}


@pytest.mark.parametrize("chunking", ["chars", "random", "whole"])
def test_item_stream_yields_each_item_once_in_order(chunking):
    text = json.dumps(DOC, ensure_ascii=False)
    if chunking == "chars":
        chunks = list(text)
    elif chunking == "random":
        rng = random.Random(7)
        chunks, i = [], 0
        while i < len(text):
            n = rng.randint(1, 9)
            chunks.append(text[i:i + n])
            i += n
    else:
        chunks = [text]
    stream = ItemStream()
    got = [item for c in chunks for item in stream.feed(c)]
    assert got == DOC["items"]
    assert stream.final() == DOC


def test_item_stream_ignores_other_arrays_and_partial_items():
    stream = ItemStream()
    assert stream.feed('{"other": [{"a": 1}], "items": [{"text": "x", "box": [1,2') == []
    assert stream.feed(',3,4]}, {"te') == [{"text": "x", "box": [1, 2, 3, 4]}]
    assert stream.final() is None                             # not complete yet


# -------------------------------------------------------------------------------------------- change gate
def frame(blocks=(), noise=0.0, seed=0):
    """A 90x160 white signature with black 10x10 blocks at the given (row, col) positions."""
    a = np.full((90, 160), 255.0, dtype=np.float32)
    for r, c in blocks:
        a[r:r + 10, c:c + 10] = 0.0
    if noise:
        a += np.random.default_rng(seed).normal(0, noise, a.shape).astype(np.float32)
    return a


def run_gate(gate, frames, dt=0.25, t0=0.0):
    out = []
    for i, f in enumerate(frames):
        out.append(gate.update(f, t0 + i * dt))
    return out


def test_signature_downscales_and_detects_changes():
    big = np.full((900, 1600, 3), 255, dtype=np.uint8)
    sig = signature(big, 160)
    assert sig.shape == (90, 160)
    assert changed_fraction(frame(), frame([(10, 10)])) == pytest.approx(100 / (90 * 160))
    assert changed_fraction(frame(), frame(noise=5.0)) == 0.0     # compression shimmer is ignored


def test_gate_fast_after_settle_then_one_deep_then_quiet():
    gate = ChangeGate(GateConfig(settle_s=0.45, deep_after_s=2.5))
    still = [frame([(10, 10)])] * 16                              # 4 s of a still picture at 4 fps
    out = run_gate(gate, still)
    assert out[:2] == [Trigger.NONE, Trigger.NONE]
    assert out[2] is Trigger.FAST                                 # 0.5 s still >= settle
    assert out.count(Trigger.FAST) == 1 and out.count(Trigger.DEEP) == 1
    assert out.index(Trigger.DEEP) == 10                          # 2.5 s still


def test_gate_stays_live_while_drawing_and_backs_off():
    gate = ChangeGate(GateConfig(max_wait_s=2.0, min_interval_s=0.8, deep_after_s=0))
    drawing = [frame([(i % 8 * 10, (i * 13) % 150)], seed=i) for i in range(80)]   # changes every tick, 20 s
    out = run_gate(gate, drawing)
    times = [i * 0.25 for i, t in enumerate(out) if t is Trigger.FAST]
    assert times[0] == pytest.approx(2.0)                         # first look after max_wait
    gaps = np.diff(times)                                         # constant motion backs off: ~4 s, then ~8 s
    assert 4.0 <= gaps[0] <= 4.5 and 8.0 <= gaps[1] <= 8.5
    assert Trigger.DEEP not in out


def test_gate_respects_min_interval_and_ignores_small_changes():
    gate = ChangeGate(GateConfig(settle_s=0.0, min_interval_s=1.0, deep_after_s=0))
    assert gate.update(frame([(0, 0)]), 0.0) is Trigger.FAST
    assert gate.update(frame([(0, 0), (50, 50)]), 0.5) is Trigger.NONE      # too soon
    assert gate.update(frame([(0, 0), (50, 50)]), 1.0) is Trigger.FAST
    tiny = frame([(0, 0), (50, 50)])
    tiny[0, 100] = 0.0                                                      # one pixel: below threshold
    assert gate.update(tiny, 3.0) is Trigger.NONE


def test_gate_never_sends_a_blank_picture():
    gate = ChangeGate(GateConfig(settle_s=0.0, min_interval_s=0.0, deep_after_s=0.5))
    assert run_gate(gate, [frame()] * 8) == [Trigger.NONE] * 8           # empty board: no fast, no deep
    stroke = frame([(30, 30)])
    assert gate.update(stroke, 3.0) is Trigger.NONE                     # first stroke appears (still moving)
    assert gate.update(stroke, 3.25) is Trigger.FAST                    # ... and settles


def test_gate_rebase_accepts_overlay_repaint_without_trigger():
    gate = ChangeGate(GateConfig(settle_s=0.0, min_interval_s=0.0, deep_after_s=0))
    assert gate.update(frame([(0, 0)]), 0.0) is Trigger.FAST
    gate.rebase_next()
    with_overlay = frame([(0, 0), (40, 40), (60, 100)])
    assert gate.update(with_overlay, 0.5) is Trigger.NONE
    assert gate.update(with_overlay, 1.0) is Trigger.NONE                   # now the reference


# ------------------------------------------------------------------------------------------------ models
def test_mark_from_raw_cleans_model_output():
    m = Mark.from_raw({"box": [50, 80, 10, -5], "text": " Haus ", "alternatives": ["Haus", "Hans", " "],
                       "kind": "error", "confidence": 3, "spot": [500, 10]}, width=100, height=60)
    assert m.box == (10.0, 0.0, 50.0, 60.0)                     # sorted and clamped
    assert m.alternatives == ["Hans"] and m.confidence == 1.0 and m.spot == (100.0, 10.0)
    assert Mark.from_raw({"box": [5, 5, 5, 9], "text": "x", "kind": "word"}, 100, 60).box is None
    assert Mark.from_raw({"box": [], "text": "", "kind": "weird"}, 100, 60) is None
    assert Mark.from_raw({"text": "Aufgabe: Fehler finden", "kind": "weird"}, 100, 60).kind == "note"


def test_schema_uses_only_supported_constraints():
    text = json.dumps(ANALYSIS_SCHEMA)
    for unsupported in ("minimum", "maximum", "minItems", "maxItems", "minLength", "maxLength"):
        assert unsupported not in text
    item = ANALYSIS_SCHEMA["properties"]["items"]["items"]
    assert set(item["required"]) == set(item["properties"]) and item["additionalProperties"] is False


# ----------------------------------------------------------------------------------------------- prompts
def test_system_prompt_is_static_and_includes_knowledge(tmp_path):
    knowledge = load_knowledge()
    assert "Verstärkung" in knowledge and "Wiener Urkunde" in knowledge
    assert knowledge.startswith("# DEK-Wissen") and "STENO_KNOWLEDGE_FILE" not in knowledge   # editor notes dropped
    a, b = build_system_prompt(knowledge), build_system_prompt(knowledge)
    assert a == b and knowledge in a and "[x1, y1, x2, y2]" in a
    custom = tmp_path / "kurs.md"
    custom.write_text("Unser Kurs: Kürzel für 'und' ist ...", encoding="utf-8")
    assert "Unser Kurs" in build_system_prompt(load_knowledge(custom))
    assert build_user_text(640, 480) == "Bildgröße: 640×480 Pixel."
    assert "Finde den Fehler" in build_user_text(640, 480, "  Finde den Fehler ")


# ------------------------------------------------------------------------------------------ layout/render
def test_place_labels_avoid_overlaps_and_stay_inside():
    anchors = [Rect(100, 100, 60, 40), Rect(170, 100, 60, 40), Rect(240, 100, 60, 40)]
    sizes = [(120.0, 30.0)] * 3
    bounds = Rect(0, 0, 600, 400)
    pos = place_labels(anchors, sizes, bounds)
    rects = [Rect(x, y, w, h) for (x, y), (w, h) in zip(pos, sizes, strict=True)]
    assert all(r.inside(bounds) for r in rects)
    for i, r in enumerate(rects):
        assert not any(r.intersects(o) for j, o in enumerate(rects) if j != i)
        assert not any(r.intersects(a) for a in anchors)


def test_label_lines_and_format():
    err = Mark(kind="error", box=(1, 2, 3, 4), text="Hans", intended="Haus", issue="Verbindung zu weit",
               fix="eng anschließen", alternatives=["Hanse"], confidence=0.4)
    lines = label_lines(err)
    assert lines[0].startswith("✗ Hans → Haus") and "40%" in lines[0]
    assert lines[1:] == ["Verbindung zu weit", "richtig: eng anschließen", "oder: Hanse"]
    assert label_lines(Mark(kind="word", text="und", confidence=0.9)) == ["und"]
    assert format_mark(err).startswith("[1,2,3,4] ✗ Hans")


def test_render_annotated_draws_on_a_margin_canvas():
    img = Image.new("RGB", (300, 120), "white")
    prep = prepare_image(img)
    result = StenoResult(frame_id=1, stage="fast", model="m", image_width=300, image_height=120, answer="ok",
                         marks=[Mark(kind="word", box=(10, 10, 60, 50), text="und"),
                                Mark(kind="note", text="Aufgabe erkannt"),
                                Mark(kind="error", box=(80, 10, 150, 60), text="x", spot=(100, 30))])
    out = render_annotated(prep, result, scale=2.0, margin=100)
    assert out.size == (600 + 2 * 33, 240 + 200) and out.mode == "RGB"


# ------------------------------------------------------------------------------------------------ hotkeys
def test_parse_hotkey_and_qt_sequence():
    assert parse_hotkey("ctrl+alt+s") == (MOD_CONTROL | MOD_ALT, ord("S"))
    assert parse_hotkey("Shift+F5") == (MOD_SHIFT, 0x74)
    assert parse_hotkey("strg-alt-3") == (MOD_CONTROL | MOD_ALT, ord("3"))
    for bad in ("", "ctrl+", "hyper+s", "ctrl+f99"):
        with pytest.raises(ValueError):
            parse_hotkey(bad)
    assert qt_sequence("ctrl+alt+s") == "Ctrl+Alt+S"
