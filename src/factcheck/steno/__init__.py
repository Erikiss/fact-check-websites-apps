"""Live stenography helper: watch a screen region (e.g. a WebEx whiteboard), read the shorthand and mark errors.

Global context: a second product next to the fact-checker, sharing its settings, logging and spend ledger.
Flow: Qt capture tick -> `frames.ChangeGate` decides when the picture is worth a look -> `engine.LiveEngine`
sends it to a fast vision model (and, once the picture is stable, to a stronger one) via `vision.py` ->
streamed marks (`models.Mark`) are drawn by the click-through overlay in `ui/` as they arrive.
Everything except `ui/` is Qt-free so it can be tested headless and reused by the `factcheck-steno image` CLI.
"""
