# Verification record

Everything in the book that claims to run was run. This file records what was
executed, on what, and what came back.

Re-run all of it with `make verify` from the repository root.

---

## Environment

| Item | Value |
|---|---|
| Machine | Apple Silicon (arm64), macOS 15.7.3 |
| Python | 3.14.6 |
| Third-party runtime dependencies | none |
| Claude Code | 2.1.233 |
| Protocol revision implemented | `2026-07-28` |
| Legacy revisions the bridge negotiates | `2025-06-18`, `2025-03-26`, `2024-11-05` |

## Test suite

```
$ python3 -m unittest discover -s meridian/tests -t .
Ran 134 tests in 2.269s
OK
```

| File | Tests | Covers |
|---|---|---|
| `tests/test_protocol.py` | 57 | stateless envelope, `resultType`, error-code allocation, caching hints, pagination, schema validation, MRTR shapes, `requestState` sealing, `x-mcp-header` rules |
| `tests/test_transports.py` | 28 | header encoding, header/body agreement, removed GET and DELETE endpoints, SSE progress streams, subscriptions, real stdio subprocesses |
| `tests/test_integration.py` | 33 | host routing and namespacing, consent, MRTR end to end, Tasks, cache behaviour, agent-loop budgets, per-server behaviour |
| `tests/test_legacy.py` | 16 | dual-era selection, legacy translation, bridge transparency |

## Benchmarks

`meridian/bench/results.json` holds the canonical run. Regenerate with
`make bench`. Every measurement printed in the book comes from that file.

Scenarios: `transport`, `coldstart`, `catalogue`, `cache`, `fanout`, `mrtr`,
`loop`, `serialisation`.

Two caveats stated wherever the numbers appear:

1. **Model inference is simulated**, from the fixed distribution in
   `meridian/host/model.py`. A book cannot ship a reproducible LLM.
2. **Transport numbers are loopback numbers.** They isolate protocol overhead
   from network physics on purpose. Add your own RTT; the book explains how.

---

## Claude Code, end to end

Claude Code 2.1.233 opens with the handshake-era protocol, so it reaches
Meridian through the dual-era bridge in `meridian/protocol/legacy.py`. This is
the arrangement described in Chapter 2 and shipped in Appendix E.

Configuration used (`.mcp.json` at the repository root):

```json
{
  "mcpServers": {
    "meridian-risk": {
      "command": "python3",
      "args": ["-m", "meridian.serve", "risk"],
      "env": {"PYTHONPATH": "."}
    },
    "meridian-fraud": {
      "command": "python3",
      "args": ["-m", "meridian.serve", "fraud"],
      "env": {"PYTHONPATH": "."}
    },
    "meridian-marketdata": {
      "command": "python3",
      "args": ["-m", "meridian.serve", "marketdata"],
      "env": {"PYTHONPATH": "."}
    }
  }
}
```

### Run 1: single server, single tool

```
$ claude -p "Use the meridian-risk MCP server: call assess_account_risk for
  accountId ACC-1042, then report ONLY the score and band, nothing else." \
  --allowedTools "mcp__meridian-risk__assess_account_risk" \
  --mcp-config .mcp.json

Score: 55.4
Band: elevated
```

### Run 2: three servers, one task

```
$ claude -p "Using the meridian MCP servers: (1) assess_account_risk for
  ACC-1042, (2) screen_account for ACC-1042 on the fraud server,
  (3) get_reference_curve for tenors 1Y and 5Y. Then output exactly three
  lines: 'RISK: <score> <band>', 'FRAUD: <verdict> <signalCount>',
  'CURVE: 1Y=<v> 5Y=<v>'." \
  --allowedTools "mcp__meridian-risk__assess_account_risk,\
mcp__meridian-fraud__screen_account,\
mcp__meridian-marketdata__get_reference_curve" \
  --mcp-config .mcp.json

RISK: 55.4 elevated
FRAUD: watch 1
CURVE: 1Y=3.96 5Y=3.68
```

Every value returned matches the fixtures directly:

| Reported | Source of truth |
|---|---|
| `55.4 elevated` | `risk_score(ACCOUNTS["ACC-1042"])` in `servers/data.py` |
| `watch 1` | one `round-amount` signal for ACC-1042 in `servers/fraud.py` |
| `1Y=3.96 5Y=3.68` | the `CURVE` constant in `servers/marketdata.py` |

### What is not verified through Claude Code, and why

**MRTR elicitation.** The multi round-trip pattern is new in `2026-07-28` and
has no handshake-era equivalent. The bridge refuses it with an explanatory
error rather than sending a result shape the client cannot parse. The pattern
is covered instead by `test_integration.TestMrtrEndToEnd`, which drives it
through the book's own client.

**The Tasks and Apps extensions.** Both require the client to opt in through
per-request capabilities. Covered by `test_integration.TestTasks`.

This split is the point of the dual-era chapter: a 2026 server has to be
correct for the protocol it targets and usable by the clients that exist.

---

## Reproducing everything

```bash
make test      # 134 tests
make bench     # regenerate meridian/bench/results.json
make verify    # both, plus the Claude Code runs above
```
