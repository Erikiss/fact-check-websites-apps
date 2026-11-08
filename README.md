# fact-check-websites-apps

A Windows app that fact-checks the claims you see — in Chrome (via an extension that underlines claims
inline) and in any desktop app such as Discord (via on-screen OCR with an overlay and a side panel).
Each claim is checked by a [Claude Agent SDK](https://code.claude.com/docs/en/agent-sdk/overview.md) agent running Haiku 5.5 with web search, page fetch,
an audited file-download tool and a sandboxed bash shell (WSL2 + bubblewrap). Every verdict uses the
four [AVeriTeC](https://arxiv.org/abs/2305.13117) labels and comes with a confidence score, the reasoning for that confidence, sources,
and — for false or cherry-picked claims — a sourced correction.

## Live stenography helper (`factcheck-steno`)

A second tool in this repo for German shorthand (DEK) courses on WebEx and similar: pick a screen region once
(the whiteboard or shared slide), and a click-through overlay shows, live, what the shorthand says, which strokes
look wrong (and why), how they should be written, and alternative readings. A fast model (Claude Haiku 5.5) reads
every new picture as soon as the lecturer stops drawing, streaming marks onto the screen as they arrive; a stronger
model (Claude Opus 5.5) takes a second look once the picture stays still. Change detection runs locally, the
overlay is hidden from screen captures on Windows, and every call goes through the project's spend ledger with a
daily cap. Needs `ANTHROPIC_API_KEY` in `.env`; `uv run factcheck-steno --fake` runs the UI at $0.
Guide (German): [`docs/steno-live.md`](docs/steno-live.md).

**Status:** work in progress. See [`reports/literature-review.md`](reports/literature-review.md) for prior work. A full README
(architecture, setup, results) follows as the components land.
