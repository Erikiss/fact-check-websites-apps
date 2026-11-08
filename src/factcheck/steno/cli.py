"""`factcheck-steno`: the live overlay (default) or a one-off analysis of a screenshot.

  factcheck-steno                      live: pick a screen region, watch it, draw readings and errors on top
  factcheck-steno live --fake          the same with $0 fake analyses (no API key needed)
  factcheck-steno image shot.png       analyse one picture; marks print as they stream in, and an annotated
                                       copy is written next to it (shot.steno-fast.png)
  factcheck-steno image shot.png --deep --hint "Finde den Fehler"

Local context: argument parsing, backend construction, the async image analysis for the `image` command.
Global context: the live mode lives in ui/app.py (imported lazily so `image` works without a display);
both use the same backend, prompts, budget ledger and logging as the rest of the project.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from PIL import Image

from factcheck.config import Settings, load_settings
from factcheck.log import setup_logging
from factcheck.pipeline.budget import BudgetExceeded, BudgetLedger
from factcheck.steno.imaging import prepare_image
from factcheck.steno.render import format_mark, render_annotated
from factcheck.steno.vision import AnthropicVision, FakeVision, StageConfig, VisionBackend, stages_from_settings


def build_backend(settings: Settings, fake: bool = False) -> VisionBackend:
    """FakeVision for --fake / STENO_FAKE=true, else the Messages API with the project's spend ledger."""
    if fake or settings.steno_fake:
        return FakeVision()
    ledger = BudgetLedger(settings.ledger_path, settings.total_budget_usd, settings.daily_app_budget_usd)
    return AnthropicVision(settings, ledger)


async def analyse_image(settings: Settings, path: Path, stages: list[StageConfig], backend: VisionBackend,
                        hint: str, out: Path | None) -> int:
    """Run each stage on one picture, streaming marks to stdout; returns the number of failed stages."""
    picture = Image.open(path)
    prepared = prepare_image(picture, settings.steno_max_image_edge, settings.steno_max_image_tokens)
    print(f"Bild {picture.width}×{picture.height} px -> Modell sieht {prepared.width}×{prepared.height} px "
          f"({prepared.tokens} Bild-Tokens)")
    failures = 0
    for cfg in stages:
        print(f"\n== {cfg.stage}: {cfg.model} (effort {cfg.effort}, thinking {cfg.thinking})", flush=True)
        try:
            result = await backend.analyze(prepared, cfg, frame_id=0, hint=hint,
                                           on_mark=lambda m: print("  " + format_mark(m), flush=True))
        except BudgetExceeded as exc:
            print(f"  übersprungen: {exc}")
            failures += 1
            continue
        if result.error:
            failures += 1
            print(f"  Fehler: {result.error}")
        print(f"  Antwort: {result.answer or '-'}")
        ttft = f"{result.ttft_s:.2f}s" if result.ttft_s is not None else "-"
        first = f"{result.first_mark_s:.2f}s" if result.first_mark_s is not None else "-"
        print(f"  erste Ausgabe {ttft} · erste Markierung {first} · gesamt {result.duration_s:.2f}s · "
              f"${result.cost_usd:.5f}")
        target = out if out is not None and len(stages) == 1 else None
        if target is None:
            stem = out.stem if out is not None else path.stem
            folder = out.parent if out is not None else path.parent
            target = folder / f"{stem}.steno-{cfg.stage}.png"
        scale = max(1.0, min(2.0, 1200 / max(1, prepared.width)))
        render_annotated(prepared, result, scale=scale).save(target)
        print(f"  annotiert: {target}")
    return failures


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="factcheck-steno", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd")
    p_live = sub.add_parser("live", help="live overlay over a screen region (default)")
    p_img = sub.add_parser("image", help="analyse one image file")
    for p in (parser, p_live):
        p.add_argument("--fake", action="store_true", help="$0 fake analyses, no API key needed")
        p.add_argument("--no-deep", action="store_true", help="fast stage only")
        p.add_argument("--region", help="x,y,w,h in screen coordinates (skips the region picker)")
    p_img.add_argument("path", type=Path)
    p_img.add_argument("--fake", action="store_true", help="$0 fake analysis, no API key needed")
    p_img.add_argument("--deep", action="store_true", help="also run the deep stage")
    p_img.add_argument("--only-deep", action="store_true", help="run only the deep stage")
    p_img.add_argument("--hint", default=None, help="context for the model, e.g. 'Finde den Fehler'")
    p_img.add_argument("--out", type=Path, default=None, help="annotated PNG (default: <image>.steno-<stage>.png)")
    args = parser.parse_args(argv)

    settings = load_settings()
    setup_logging(settings.logs_dir, name="steno", echo_llm=False)
    if args.cmd == "image":
        stages = stages_from_settings(settings)
        wanted = ["deep"] if args.only_deep else (["fast", "deep"] if args.deep else ["fast"])
        missing = [s for s in wanted if s not in stages]
        if missing:
            sys.exit(f"Stufe {missing} ist nicht konfiguriert (STENO_DEEP_MODEL in .env setzen)")
        backend = build_backend(settings, fake=args.fake)
        hint = settings.steno_hint if args.hint is None else args.hint
        failures = asyncio.run(analyse_image(settings, args.path, [stages[s] for s in wanted], backend, hint,
                                             args.out))
        sys.exit(1 if failures else 0)

    from factcheck.steno.ui.app import run_live  # Qt is only needed for the live mode

    sys.exit(run_live(settings, fake=args.fake, deep=not args.no_deep, region=args.region))


if __name__ == "__main__":
    main()
