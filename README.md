# Building Awesome MCP Apps

### *What every AI application developer should know about the Model Context Protocol*

**Read it online: [krimler.github.io/Building-Awesome-MCP-Apps](https://krimler.github.io/Building-Awesome-MCP-Apps/)**

A protocol book that is secretly a performance book. It teaches the Model Context
Protocol down to the bytes on the wire, then cashes that understanding out into
the three budgets every agentic application spends: **latency**, **context**, and
**cost**.

Written against MCP revision **`2026-07-28`**, the largest change since MCP
launched: a stateless core, multi round-trip requests, the extensions framework,
and a formal feature-lifecycle policy.

---

## What is in here

| | |
|---|---|
| **20 chapters + epilogue**, five parts | ~48,000 words, 312 pages |
| **[`meridian/`](meridian/)** | A complete `2026-07-28` implementation, no third-party runtime dependencies |
| **200 tests** | Contract, transport, integration, and dual-era. About two seconds |
| **9 benchmark scenarios** | Every number printed in the book comes from these |
| **25 figures** | TikZ and matplotlib, generated to PDF and SVG from one source |

Every listing in the book is extracted from Meridian. Every measurement is
reproducible with `make bench`.

## Quick start

```bash
git clone https://github.com/krimler/Building-Awesome-MCP-Apps
cd Building-Awesome-MCP-Apps

make test      # 200 tests, about two seconds
make bench     # regenerate every measurement in the book
```

Python 3.11 or later. Nothing else for the code.

### Run the servers

```bash
python3 -m meridian.serve risk                 # dual-era stdio
python3 -m meridian.serve all --http 8931      # all four over HTTP
python3 -m meridian.serve risk --fat           # the pre-Chapter-5 catalogue
python3 -m meridian.serve compliance --poisoned  # the Chapter 19 scenario
```

### With Claude Code

The repository ships a `.mcp.json`:

```bash
claude -p "Assess ACC-1042 for risk, screen it for fraud, and get the 1Y
and 5Y reference rates." --mcp-config .mcp.json
```

```
RISK:  55.4 elevated
FRAUD: watch 1
CURVE: 1Y=3.96 5Y=3.68
```

Those values come straight from the fixtures. See
[`meridian/VERIFICATION.md`](meridian/VERIFICATION.md) for the full record,
including what is *not* verifiable this way and why.

## Contents

**Part I — Protocol Fundamentals.** The physics of agent loops, the stateless
architectural model, both transports byte by byte, and OAuth 2.1 as MCP profiles it.

**Part II — The Primitives.** Tools, resources, prompts, multi round-trip
requests, the extensions framework with Tasks, and MCP Apps. Each chapter follows
the same pattern: wire mechanics, then cost, then guidance.

**Part III — Building MCP Applications.** Servers, hosts and clients, the agent
loop, and multi-agent topologies.

**Part IV — Performance Engineering.** Testing, measuring, optimising, operating.

**Part V — Security and Trust.** Threat modelling and access control.

Plus five appendices: a method reference, a transport decision matrix, and
one-page performance and security checklists.

## A sample of the measurements

From `make bench`, on Apple Silicon, Python 3.14:

| | Before | After |
|---|---:|---:|
| Loop iterations per task | 4 | 2 |
| Wall clock | 1,753 ms | 1,014 ms |
| Tokens per task | 22,846 | 2,692 |
| Cost per task | $0.0690 | $0.0086 |
| Setup requests over 20 tasks | 164 | 8 |

Four changes, each attributed to a specific technique in Chapter 17. No new
hardware, no faster language.

**Two caveats, stated wherever the numbers appear.** Model inference latency and
token pricing are *modelled*, from the constants in `meridian/host/model.py`.
Everything downstream of inference is *measured*. Transport figures are loopback
figures on purpose, to isolate protocol overhead from network physics.

## Building the book

```bash
make figures   # regenerate every figure to PDF and SVG
make book      # the PDF
make site      # the website into docs/
make lint      # prose rules and cross-reference integrity
make verify    # tests, benchmarks, and the Claude Code runs
make           # figures, book, and site
```

Needs a TeX distribution, `pandoc`, and matplotlib (`.venv/` is used if present).

The website is generated from the same LaTeX as the PDF, so there is exactly one
copy of every sentence in this book. Figures are built to both formats from one
source, so print and web cannot drift.

## Repository layout

```
book/            LaTeX source, one file per chapter
figures-src/     figure sources: TikZ and matplotlib
meridian/        the companion application
  protocol/        ~3,900 lines: a complete 2026-07-28 implementation
  servers/         risk, compliance, fraud, marketdata
  host/            Host, AgentLoop, and a deterministic model stub
  bench/           the measurement harness and results.json
  tests/           200 tests
docs/            the generated website (GitHub Pages)
tools/           build, lint, and verification scripts
```

## Contributing

Corrections welcome, and **reproduction failures most of all**. The entire
premise is that the numbers are checkable, so a number you cannot reproduce is
the most useful bug report available. Include your platform, your Python version,
and the output of `make bench`.

## Licence

Copyright © 2026 Krimler. All rights reserved.

The cover is generated by `figures-src/plots/cover.py`, so its provenance is
unambiguous and no third-party artwork is involved.
