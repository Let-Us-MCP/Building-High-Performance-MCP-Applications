# Building Awesome MCP Apps — working notes

Live progress log for the book. Updated as work lands.

**Target repo:** https://github.com/krimler/Building-Awesome-MCP-Apps (account `krimler`, email `yavan@outlook.com`)
**Spec pinned to:** MCP revision `2026-07-28`
**Reference material:** `proto/modelcontextprotocol` (git clone of the spec repo; never committed)

---

## House style (from `desc`, binding on every chapter)

1. **Steve Yegge block style.** Long-form, conversational, opinionated, funny where it earns
   the laugh. Direct address to the reader. Strong claims, then the evidence.
2. **Lucid prose, heavy on examples.** Every idea gets a concrete example, a diagram, or a
   measurement. Preferably all three.
3. **No em dashes.** Not one. Rewrite the sentence instead. `tools/lint_prose.py` enforces this.
4. **No AI slop.** No "it's important to note", no "in today's fast-paced world", no
   tricolon padding, no restating the paragraph you just wrote. `tools/lint_prose.py` flags
   a banned-phrase list and near-duplicate sentences.
5. **Movie dialogue epigraph** opens every chapter, chosen to actually land on the chapter's
   argument rather than for decoration.
6. **xkcd-style figures** alongside the technical diagrams. Hand-drawn look, dry joke, real point.
7. **Everything runs.** Code in the book comes from `meridian/`, which is tested. Numbers in
   the book come from `meridian/bench/`, which is run.

## Architecture

```
book/           LaTeX source, one .tex per chapter (multi-file setup)
  book.tex        master document
  preamble.tex    packages, macros, theorem-ish environments (legacybox, xkcdfig, ...)
  chapters/       ch01..ch20, epilogue
  appendices/     appA..appE
  frontmatter/    cover, title, copyright, preface, about the author
  figures/        GENERATED — do not hand-edit
figures-src/    figure sources: TikZ (.tex) and matplotlib xkcd (.py)
meridian/       the instrumented reference application (companion code)
  protocol/       zero-dependency 2026-07-28 implementation (the book's wire layer)
  servers/        risk, compliance, fraud, marketdata
  host/           minimal host + client + agent loop
  bench/          measurement harness that produces the book's numbers
  tests/          contract + unit tests
site/           GENERATED GitHub Pages site
tools/          build + lint scripts
```

**Figure pipeline.** Every figure is generated to *both* PDF (for LaTeX) and SVG (for the
website) by `tools/build_figures.py`. TikZ sources compile via `pdflatex` + `dvisvgm --pdf`;
matplotlib sources save both formats directly. Nothing is hand-drawn, so nothing drifts.

**Website pipeline.** LaTeX is the single source of truth. `tools/build_site.py` runs pandoc
per chapter with a custom filter, then wraps the output in a hand-written responsive template.
No Jekyll, no Ruby, no theme fighting.

---

## Progress

Legend: `[ ]` not started · `[~]` drafted · `[x]` drafted + prose-linted + built

### Infrastructure
- [x] Repo skeleton, `.gitignore`, Makefile
- [x] LaTeX preamble, master document, multi-file chapter setup
- [x] Prose linter (`tools/lint_prose.py`): em dashes, slop phrases, repetition
- [x] Figure build pipeline (TikZ + matplotlib xkcd → PDF + SVG)
- [x] Cover (generated abstract art, our own work, so copyright-clean)
- [x] Website generator + template
- [x] GitHub Actions workflow for Pages

### Companion code (`meridian/`)
- [x] `protocol/` — JSON-RPC, `_meta` envelope, resultType, error codes
- [x] `protocol/` — stdio transport
- [x] `protocol/` — Streamable HTTP transport (POST-only, SSE response streams, Mcp-* headers)
- [x] `protocol/` — `server/discover`, capabilities, CacheableResult
- [x] `protocol/` — MRTR (`input_required`, `inputRequests`, signed `requestState`)
- [x] `protocol/` — `subscriptions/listen`
- [x] `protocol/` — Tasks extension
- [x] `servers/risk`, `servers/compliance`, `servers/fraud`, `servers/marketdata`
- [x] `host/` — client, connection pool, capability cache, agent loop
- [x] `bench/` — measurement harness
- [x] `tests/` — 196 tests, all passing
- [x] Verified end-to-end with Claude Code (`.mcp.json` + transcript in `meridian/VERIFICATION.md`)

### Part I — Protocol Fundamentals
- [x] Ch 1  Primer on Latency, Tokens, and Context
- [x] Ch 2  The MCP Architectural Model
- [x] Ch 3  Building Blocks of the Transport
- [x] Ch 4  Authorization on the Wire

### Part II — The Primitives
- [x] Ch 5  Tools: The Execution Layer
- [x] Ch 6  Resources: The Context Layer
- [x] Ch 7  Prompts: Workflow Blueprints
- [x] Ch 8  Multi Round-Trip Requests
- [x] Ch 9  Extensions and Tasks
- [x] Ch 10 MCP Apps

### Part III — Building MCP Applications
- [x] Ch 11 Building Servers
- [x] Ch 12 Building Hosts and Clients
- [x] Ch 13 The Agent Loop, State, and Memory
- [x] Ch 14 Multi-Agent Topologies

### Part IV — Performance Engineering
- [x] Ch 15 Testing and Debugging
- [x] Ch 16 Measuring MCP Applications
- [x] Ch 17 Optimizing for Latency, Tokens, and Cost
- [x] Ch 18 Deployment and Operations

### Part V — Security and Trust
- [x] Ch 19 Threat Modeling Agentic Applications
- [x] Ch 20 Access Control, Audit, and MCP in the Enterprise

### Back matter
- [x] Epilogue
- [x] App A  Method and message reference
- [x] App B  Transport decision matrix
- [x] App C  Performance checklist
- [x] App D  Security hardening checklist
- [x] App E  The Meridian companion repository

### Publish
- [x] Full PDF builds clean
- [x] Site builds
- [x] Pushed to GitHub + Pages enabled

---

## Decisions and their reasons

- **Zero-dependency protocol implementation.** The SDKs do not yet ship `2026-07-28`
  (no `server/discover`, no MRTR, no `resultType`). A book that teaches the wire should
  show the wire. `meridian/protocol/` is about 2,000 lines of stdlib Python and doubles as
  the reference implementation for every listing in the book.
- **Measurements are real.** Every number printed in the book comes from
  `meridian/bench/`, run on the machine described in Chapter 1, and regenerated by
  `make bench`. Model-inference timings are simulated by a deterministic stub whose latency
  distribution is stated up front, because a book cannot ship a reproducible LLM.
  Everything downstream of inference (serialization, transport, server execution, caching)
  is measured for real.
- **Generated cover.** "Copyright-free abstract image" is most reliably satisfied by making
  one. `figures-src/cover.py` draws it, so provenance is unambiguous and the source ships.
- **Website from LaTeX, not a parallel Markdown copy.** Two sources drift. One source with
  a converter does not. `tools/build_site.py` reads chapter numbering out of
  `book/build/book.aux`, so "Chapter 5" on the web is provably the same Chapter 5 as in
  the PDF.
- **Benchmark numbers drifted once, and now cannot.** Regenerating `results.json` after
  adding the Chapter 17 optimisation scenario moved a dozen timings, and the prose kept
  quoting the old ones. Caught by rendering a page and comparing it with its own figure.
  `tools/check_numbers.py` now extracts every canonical value from `results.json` and
  fails if it appears nowhere in the book; it accepts sensible roundings. Committed
  `results.json` is the canonical run, and prose and results are updated together.
- **Meridian ships a dual-era bridge, and this changed the book.** Discovered while
  verifying against Claude Code 2.1.233: it opens with `initialize`, so a strict
  2026-07-28 server is correct and completely unusable. `protocol/legacy.py` serves both
  eras from one process, exactly as the spec's compatibility matrix describes. This is now
  a real thread through Chapters 2, 3, and 12 rather than a footnote, because it is the
  situation every reader shipping in 2026 is actually in. MRTR has no legacy equivalent,
  so the bridge refuses it with an explanatory error instead of sending a shape the client
  cannot parse; that limitation is documented rather than hidden.

## Style rules discovered in review (binding from here on)

- **Write straight, not contrastively.** The first draft leaned on "X is not Y. It is
  Z.", "not just A but B", and "rather than" as a default connective, which reads as
  arguing with somebody who is not in the room. Say what the thing IS.
  `tools/check_contrastive.py` counts them; budget is 8 per file. Went 244 -> 163.
- **Define before use, never defer.** The first draft used "elicitation" in a table
  before defining it anywhere, and waved at concepts with "Chapter 8 covers the
  mechanism properly". `tools/audit_style.py --deferrals` catches that; it is now 0.
- **Density over sparseness.** HPBN builds a concept fully before spending it. Ch 1-3
  were rewritten on that model and roughly doubled: ch01 1,584 -> 3,790 words of prose.
- **Every listing must trace to real code.** `tools/check_listings.py` proved ~15
  listings were sketches the book presented as extracted. Fixed by implementing them
  (`host/delegation.py`, `servers/scoped.py`, `ops.py`, `client.call_with_reauth`) and
  marking the genuinely elided ones `# sketch:`. Now 151/151 trace.

## Open items / notes for the author

- `desc` line 10 says "lipic style". Read as *lucid* style: plain, concrete, example-first.
  Change `tools/lint_prose.py` and this note if a different reading was intended.
- Movie epigraphs are short quoted fragments used for commentary, which is ordinary fair
  use for a technical book, but a publisher's legal review may want them swapped. They are
  isolated in `\epigraph{}{}` calls, one per chapter, so swapping is mechanical.
