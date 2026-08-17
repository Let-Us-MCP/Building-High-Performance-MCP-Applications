# Building High-Performance MCP Applications — working notes

Live progress log for the book. Updated as work lands.

**Target repo:** https://github.com/Let-Us-MCP/Building-High-Performance-MCP-Applications (account `krimler`, email `yavan@outlook.com`)
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
  appendices/     appA..appF
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
- [x] `tests/` — 208 tests, all passing (`tools/check_counts.py` keeps the book's claim honest)
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
- [x] App F  Sources and further reading

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
- **Density over sparseness.** HPBN builds a concept fully before spending it. Every
  chapter was rewritten on that model. Chapters used to run 1,600-2,600 words; they now
  run 2,000-4,900, and the additions are all mechanism rather than padding.
- **Every listing must trace to real code.** `tools/check_listings.py` proved ~15
  listings were sketches the book presented as extracted. Fixed by implementing them
  rather than weakening the claim, and the same rule then drove most of the second pass:
  every gap the audit found in the prose was closed by writing the code first.
- **Facts about the companion code go stale silently.** `tools/check_counts.py` runs
  unittest discovery and compares the result with what the book claims. It found the
  book saying "134 tests" in twelve places, having been true about sixty tests earlier,
  and has caught the number four more times since.

## What the define-before-use pass added

Each of these was a concept the prose used as though the reader already had it. The
fix in every case was to explain the mechanism, and where the book had no code behind
the claim, to write the code.

| Chapter | Was asserted | Now explained, and what shipped |
|---|---|---|
| 4  | "PKCE, always" | verifier/challenge, the interception attack, the MUST to verify support first |
| 4  | "audience binding" | the `aud` claim, then the confused deputy; mix-up described as an attack |
| 5  | "destroys the prompt cache" | what a prompt cache keys on, and why your own latency graph stays flat |
| 5  | cursors, annotations | opaque cursors, the empty-string cursor bug, all four annotations and their defaults |
| 6  | resource descriptors | the field table, `size` as a context-budget control, RFC 6570's four levels |
| 7  | few-shot, completions | multi-message prompts; `completion/complete` implemented + 7 tests |
| 8  | "HMAC", "constant time" | what a MAC proves; the timing attack with numbers; `v1.` prefix as key rotation |
| 9  | polling only | `notifications/tasks` implemented + 5 tests; why the push carries the whole task |
| 10 | sandbox, CSP, postMessage | the allow-same-origin trap, the directive table, where the origin check goes |
| 11 | "store it before returning" | `protocol/idempotency.py` + 8 tests: fingerprints, concurrency, failure, expiry |
| 12 | `traceparent`, "add jitter" | the four fields; `ops.Backoff` with full jitter + reset + 5 tests |
| 13 | context cost | position effects and accumulated contradictions, which decide more than cost |
| 14 | "token exchange exists" | RFC 8693 in full; delegation cycle detection + 5 tests |
| 15 | "non-deterministic" | why temperature 0 does not fix it |
| 16 | percentiles | tail amplification: fan-out turns your p99 into your typical case |
| 17 | reuse + fan out | **a real bug**: one HTTP/1.1 connection serialised the fan-out. Pool + 4 tests |
| 18 | capacity arithmetic | Little's Law, sized on burst rate and degraded service time |
| 20 | "append-only" | `meridian/audit.py` hash chain + 8 tests, and the limitation stated

## Open items / notes for the author

- `desc` line 10 says "lipic style". Read as *lucid* style: plain, concrete, example-first.
  Change `tools/lint_prose.py` and this note if a different reading was intended.
- Movie epigraphs are short quoted fragments used for commentary, which is ordinary fair
  use for a technical book, but a publisher's legal review may want them swapped. They are
  isolated in `\epigraph{}{}` calls, one per chapter, so swapping is mechanical.
