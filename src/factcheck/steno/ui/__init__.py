"""Qt (PySide6) side of the live steno mode: region picker, click-through overlay, control bar, details panel.

Global context: the only part of `factcheck.steno` that needs a display; started by `factcheck-steno` via
`ui.app.run_live`. The windows exclude themselves from screen capture on Windows, so the model never sees the
overlay it is annotating (winapi.py).
"""
