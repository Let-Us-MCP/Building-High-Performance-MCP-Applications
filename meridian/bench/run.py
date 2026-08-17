"""The measurement harness. Every number in the book comes out of here.

    python3 -m meridian.bench.run                 # run everything, print a table
    python3 -m meridian.bench.run --json out.json # machine-readable
    python3 -m meridian.bench.run transport cache # named scenarios only

What is measured for real: serialisation, transport, server execution, cache
behaviour, round-trip counts, catalogue token cost, cold-start time.

What is modelled: model inference latency and token pricing, from the fixed
distribution in `meridian/host/model.py`. Chapter 1 states that distribution so
you can disagree with it without having to guess what it was.
"""

from __future__ import annotations

import argparse
import json
import platform
import statistics
import subprocess
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from ..host.host import Host
from ..host.loop import AgentLoop
from ..host.model import StubModel, estimate_tokens
from ..protocol import (
    Client,
    ClientCapabilities,
    InProcessTransport,
    ScriptedInput,
    StdioClientTransport,
    StreamableHttpClient,
    StreamableHttpServer,
)
from ..protocol import tasks as tasks_ext
from ..servers import compliance, fraud, marketdata, risk


# ---------------------------------------------------------------------------
# Timing helpers
# ---------------------------------------------------------------------------


def percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, int(round((p / 100.0) * (len(ordered) - 1))))
    return ordered[idx]


@dataclass
class Timing:
    label: str
    samples: list[float] = field(default_factory=list)

    def add(self, ms: float) -> None:
        self.samples.append(ms)

    @property
    def mean(self) -> float:
        return statistics.fmean(self.samples) if self.samples else 0.0

    def to_json(self) -> dict:
        return {
            "label": self.label,
            "n": len(self.samples),
            "meanMs": round(self.mean, 3),
            "p50Ms": round(percentile(self.samples, 50), 3),
            "p95Ms": round(percentile(self.samples, 95), 3),
            "p99Ms": round(percentile(self.samples, 99), 3),
            "minMs": round(min(self.samples), 3) if self.samples else 0,
            "maxMs": round(max(self.samples), 3) if self.samples else 0,
        }


def time_it(fn: Callable[[], Any], *, n: int = 200, warmup: int = 20,
            label: str = "") -> Timing:
    for _ in range(warmup):
        fn()
    timing = Timing(label)
    for _ in range(n):
        started = time.perf_counter()
        fn()
        timing.add((time.perf_counter() - started) * 1000.0)
    return timing


# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------

SCENARIOS: dict[str, Callable[[], dict]] = {}


def scenario(name: str):
    def deco(fn):
        SCENARIOS[name] = fn
        return fn
    return deco


@scenario("transport")
def bench_transport() -> dict:
    """In-process versus stdio versus Streamable HTTP, same call, same server.

    Subtracting the in-process figure from the others gives you transport cost
    with server execution removed. That subtraction is the point of the whole
    scenario.
    """
    args = {"name": "assess_account_risk", "arguments": {"accountId": "ACC-1042"}}

    inproc_server = risk.build_server()
    inproc = Client(InProcessTransport(inproc_server), server_label="inproc")
    inproc.list_tools()
    t_inproc = time_it(lambda: inproc.call_tool(**_split(args)),
                       n=400, label="in-process")

    http_server = StreamableHttpServer(risk.build_server(), port=0).start()
    try:
        http_client = Client(StreamableHttpClient(http_server.url), server_label="http")
        http_client.list_tools()
        t_http = time_it(lambda: http_client.call_tool(**_split(args)),
                         n=300, label="streamable http")
        http_client.close()
    finally:
        http_server.stop()

    stdio_transport = StdioClientTransport(
        [sys.executable, "-m", "meridian.servers.risk"])
    try:
        stdio_client = Client(stdio_transport, server_label="stdio")
        stdio_client.list_tools()
        t_stdio = time_it(lambda: stdio_client.call_tool(**_split(args)),
                          n=300, label="stdio")
    finally:
        stdio_transport.close()

    return {
        "results": [t.to_json() for t in (t_inproc, t_stdio, t_http)],
        "derived": {
            "stdioOverheadMs": round(t_stdio.mean - t_inproc.mean, 3),
            "httpOverheadMs": round(t_http.mean - t_inproc.mean, 3),
            "serverExecutionMs": round(t_inproc.mean, 3),
        },
    }


def _split(call: dict) -> dict:
    return {"name": call["name"], "arguments": call["arguments"]}


@scenario("coldstart")
def bench_coldstart() -> dict:
    """What a stdio server costs before it answers anything.

    This is the number that decides between spawn-per-call and a warm pool. It
    is dominated by interpreter start plus imports, and it is much larger than
    people expect.
    """
    spawn_times: list[float] = []
    first_call_times: list[float] = []

    for _ in range(12):
        started = time.perf_counter()
        transport = StdioClientTransport(
            [sys.executable, "-m", "meridian.servers.fraud"])
        client = Client(transport, server_label="fraud")
        client.discover()
        spawn_times.append((time.perf_counter() - started) * 1000.0)

        started = time.perf_counter()
        client.call_tool("screen_account", {"accountId": "ACC-1000"})
        first_call_times.append((time.perf_counter() - started) * 1000.0)
        transport.close()

    warm = StdioClientTransport([sys.executable, "-m", "meridian.servers.fraud"])
    try:
        warm_client = Client(warm, server_label="fraud")
        warm_client.list_tools()
        t_warm = time_it(
            lambda: warm_client.call_tool("screen_account", {"accountId": "ACC-1000"}),
            n=200, label="warm call")
    finally:
        warm.close()

    cold = Timing("cold start to first response")
    cold.samples = spawn_times
    return {
        "results": [cold.to_json(), t_warm.to_json()],
        "derived": {
            "coldStartMs": round(cold.mean, 1),
            "warmCallMs": round(t_warm.mean, 3),
            "callsToAmortiseColdStart": round(cold.mean / max(t_warm.mean, 0.001)),
        },
    }


@scenario("catalogue")
def bench_catalogue() -> dict:
    """What a tool catalogue costs, per model turn, forever.

    Tool descriptions are not paid once at startup. They are re-sent on every
    single request, which makes catalogue size a recurring tax rather than a
    fixed cost.
    """
    slim = _wire(fat=False)
    fat = _wire(fat=True)

    slim_tokens = slim.catalogue_tokens()
    fat_tokens = fat.catalogue_tokens()

    plan = [[{"name": "risk.assess_account_risk",
              "arguments": {"accountId": "ACC-1042"}}], []]
    slim_run = AgentLoop(slim, StubModel(plan=plan, simulate_latency=False)).run("go")
    fat_run = AgentLoop(fat, StubModel(plan=plan, simulate_latency=False)).run("go")

    from ..host.model import INPUT_USD_PER_MTOK

    per_turn_delta = fat_tokens - slim_tokens
    return {
        "results": [
            {"label": "slim catalogue", "tools": len(slim.catalogue()),
             "tokens": slim_tokens},
            {"label": "fat catalogue", "tools": len(fat.catalogue()),
             "tokens": fat_tokens},
        ],
        "derived": {
            "extraTokensPerTurn": per_turn_delta,
            "extraTokensPerTask": fat_run.total_tokens - slim_run.total_tokens,
            "extraUsdPer1000Tasks": round(
                (fat_run.total_tokens - slim_run.total_tokens)
                * INPUT_USD_PER_MTOK / 1_000_000.0 * 1000, 4),
            "catalogueRatio": round(fat_tokens / max(slim_tokens, 1), 2),
        },
    }


def _wire(*, fat: bool = False, inputs: dict | None = None,
          shared_cache: bool = True, **kw) -> Host:
    host = Host(shared_cache=shared_cache, **kw)
    provider = ScriptedInput(inputs or {})
    risk_server = risk.build_server(fat_catalogue=fat)
    tasks_ext.install(risk_server)
    host.connect("risk", InProcessTransport(risk_server), input_provider=provider)
    host.connect("compliance", InProcessTransport(compliance.build_server()),
                 input_provider=provider)
    host.connect("fraud", InProcessTransport(fraud.build_server()),
                 input_provider=provider)
    host.connect("marketdata", InProcessTransport(marketdata.build_server()),
                 input_provider=provider)
    host.refresh_catalogue()
    return host


@scenario("cache")
def bench_cache() -> dict:
    """What honouring `ttlMs` is worth over a realistic access pattern."""
    server = marketdata.build_server()

    cold = Client(InProcessTransport(server), server_label="md")
    cold.cache = None  # measure with caching switched off
    from ..protocol import ResultCache

    class NoCache(ResultCache):
        def get(self, *a, **kw): return None
        def put(self, *a, **kw): return False

    cold.cache = NoCache()
    t_cold = time_it(lambda: cold.list_tools(), n=300, label="tools/list uncached")

    warm = Client(InProcessTransport(server), server_label="md")
    warm.list_tools()
    t_warm = time_it(lambda: warm.list_tools(), n=300, label="tools/list cached")

    # A mixed read pattern: 40 distinct resources, Zipf-ish repetition.
    import random
    rng = random.Random(11)
    uris = [r["uri"] for r in warm.list_resources()][:40]
    pattern = [uris[min(len(uris) - 1, int(rng.paretovariate(1.4)) - 1)]
               for _ in range(600)]

    plain = Client(InProcessTransport(server), server_label="md2")
    plain.cache = NoCache()
    started = time.perf_counter()
    for uri in pattern:
        plain.read_resource(uri)
    uncached_ms = (time.perf_counter() - started) * 1000.0

    cached = Client(InProcessTransport(server), server_label="md3")
    started = time.perf_counter()
    for uri in pattern:
        cached.read_resource(uri)
    cached_ms = (time.perf_counter() - started) * 1000.0

    return {
        "results": [t_cold.to_json(), t_warm.to_json()],
        "derived": {
            "readsIssued": len(pattern),
            "uncachedMs": round(uncached_ms, 1),
            "cachedMs": round(cached_ms, 1),
            "requestsAvoided": len(pattern) - cached.stats.requests,
            "hitRatePct": round(100 * cached.cache.stats.hit_rate, 1),
            "speedup": round(uncached_ms / max(cached_ms, 0.001), 2),
        },
    }


@scenario("fanout")
def bench_fanout() -> dict:
    """Serial versus parallel tool calls, with a realistic per-call latency.

    The model asked for three tools in one turn, which is its way of saying the
    three are independent. Running them in sequence discards that information.
    """
    calls = [
        {"name": "risk.assess_account_risk", "arguments": {"accountId": "ACC-1042"}},
        {"name": "fraud.screen_account", "arguments": {"accountId": "ACC-1042"}},
        {"name": "marketdata.get_reference_curve", "arguments": {}},
    ]

    out = []
    for latency in (2.0, 10.0, 40.0):
        host = _wire()
        for binding in host.bindings.values():
            binding.client.transport.latency_ms = latency

        def serial_run(h=host):
            return [h.call_tool(c["name"], c["arguments"]) for c in calls]

        def parallel_run(h=host):
            return h.call_tools_parallel(calls)

        serial = time_it(serial_run, n=20, warmup=3, label=f"serial @{latency:.0f}ms")
        parallel = time_it(parallel_run, n=20, warmup=3,
                           label=f"parallel @{latency:.0f}ms")

        out.append({
            "perCallLatencyMs": latency,
            "serialMs": round(serial.mean, 2),
            "parallelMs": round(parallel.mean, 2),
            "savedMs": round(serial.mean - parallel.mean, 2),
            "speedup": round(serial.mean / max(parallel.mean, 0.001), 2),
        })

    return {"results": out, "derived": {"calls": len(calls)}}


@scenario("mrtr")
def bench_mrtr() -> dict:
    """What an extra round trip actually costs a task.

    Two versions of the same work. One asks for approval mid-call, the other
    collects it in the tool schema up front. Same outcome, different number of
    trips through the model.
    """
    host = _wire(inputs={
        "approval": {"action": "accept", "content": {"approver": "j.okonjo"}},
    })
    for binding in host.bindings.values():
        binding.client.transport.latency_ms = 12.0

    binding = host.bindings["risk"]

    def with_mrtr():
        return host.call_tool("risk.assess_account_risk",
                              {"accountId": "ACC-1042", "exposureUsd": 9_000_000})

    def without_mrtr():
        return host.call_tool("risk.assess_account_risk",
                              {"accountId": "ACC-1042", "exposureUsd": 100_000})

    before = binding.client.stats.requests
    t_with = time_it(with_mrtr, n=40, warmup=5, label="with elicitation")
    mrtr_requests = binding.client.stats.requests - before

    before = binding.client.stats.requests
    t_without = time_it(without_mrtr, n=40, warmup=5, label="no elicitation")
    plain_requests = binding.client.stats.requests - before

    # Add one model turn, which is what a real host pays to render and read a form.
    from ..host.model import TTFT_MEAN_MS

    return {
        "results": [t_without.to_json(), t_with.to_json()],
        "derived": {
            "extraTransportMs": round(t_with.mean - t_without.mean, 2),
            "requestsPerCallWithMrtr": round(mrtr_requests / 45, 2),
            "requestsPerCallWithout": round(plain_requests / 45, 2),
            "estimatedTotalCostMs": round(t_with.mean - t_without.mean + TTFT_MEAN_MS, 1),
        },
    }


@scenario("loop")
def bench_loop() -> dict:
    """A full agent loop, decomposed. This is the Chapter 1 waterfall."""
    host = _wire()
    for binding in host.bindings.values():
        binding.client.transport.latency_ms = 6.0

    plan = [
        [{"name": "risk.assess_account_risk", "arguments": {"accountId": "ACC-1042"}}],
        [{"name": "fraud.screen_account", "arguments": {"accountId": "ACC-1042"}},
         {"name": "marketdata.get_reference_curve", "arguments": {}}],
        [],
    ]

    serial = AgentLoop(host, StubModel(plan=plan, seed=3, simulate_latency=False),
                       parallel_fanout=False).run("Full review of ACC-1042.")
    parallel = AgentLoop(host, StubModel(plan=plan, seed=3, simulate_latency=False),
                         parallel_fanout=True).run("Full review of ACC-1042.")

    return {
        "results": [
            {"label": "serial fan-out", **serial.to_json()},
            {"label": "parallel fan-out", **parallel.to_json()},
        ],
        "derived": {
            "modelSharePct": serial.to_json()["modelSharePct"],
            "transportSharePct": round(
                100.0 * serial.transport_ms / serial.wall_ms, 1) if serial.wall_ms else 0,
            "savedMs": round(serial.wall_ms - parallel.wall_ms, 1),
        },
    }


@scenario("optimization")
def bench_optimization() -> dict:
    """The Chapter 17 pass: one task, six changes, measured one at a time.

    Each stage is cumulative, so the last row is everything applied together.
    The point is the attribution: each line names the change and what it bought,
    rather than presenting one heroic before-and-after.
    """
    PLAN_SERIAL = [
        [{"name": "risk.assess_account_risk", "arguments": {"accountId": "ACC-1042"}}],
        [{"name": "fraud.screen_account", "arguments": {"accountId": "ACC-1042"}}],
        [{"name": "marketdata.get_reference_curve", "arguments": {}}],
        [],
    ]
    PLAN_BATCHED = [
        [{"name": "risk.assess_account_risk", "arguments": {"accountId": "ACC-1042"}},
         {"name": "fraud.screen_account", "arguments": {"accountId": "ACC-1042"}},
         {"name": "marketdata.get_reference_curve", "arguments": {}}],
        [],
    ]

    LATENCY = 12.0  # a plausible same-datacentre hop

    def build(*, fat: bool, latency: float, cache: bool) -> Host:
        host = _wire(fat=fat, shared_cache=cache)
        for binding in host.bindings.values():
            binding.client.transport.latency_ms = latency
        return host

    def run(host: Host, plan, *, parallel: bool, seed: int = 5):
        return AgentLoop(host, StubModel(plan=plan, seed=seed,
                                         simulate_latency=False),
                         parallel_fanout=parallel).run("Full review of ACC-1042.")

    stages = []

    # 0. Baseline: fat catalogue, serial plan, no fan-out, cold cache.
    host = build(fat=True, latency=LATENCY, cache=False)
    base = run(host, PLAN_SERIAL, parallel=False)
    stages.append(("baseline", base, "38-tool catalogue, one call per turn"))

    # 1. Trim the catalogue (Chapter 5).
    host = build(fat=False, latency=LATENCY, cache=False)
    s1 = run(host, PLAN_SERIAL, parallel=False)
    stages.append(("trim the catalogue", s1, "38 tools -> 10"))

    # 2. Let the model batch independent calls into one turn (Chapter 5).
    host = build(fat=False, latency=LATENCY, cache=False)
    s2 = run(host, PLAN_BATCHED, parallel=False)
    stages.append(("batch into one turn", s2, "4 iterations -> 2"))

    # 3. Fan out what was batched (Chapter 5).
    host = build(fat=False, latency=LATENCY, cache=False)
    s3 = run(host, PLAN_BATCHED, parallel=True)
    stages.append(("fan out in parallel", s3, "sum of latencies -> max"))

    rows = []
    baseline_wall = stages[0][1].wall_ms
    baseline_tok = stages[0][1].total_tokens
    for name, result, note in stages:
        rows.append({
            "label": name,
            "note": note,
            "iterations": len(result.iterations),
            "roundTrips": result.round_trips,
            "wallMs": round(result.wall_ms, 1),
            "transportMs": round(result.transport_ms, 1),
            "tokens": result.total_tokens,
            "costUsd": round(result.cost_usd, 6),
        })

    # The catalogue refresh sits outside the loop, so caching does not move the
    # numbers above at all. What it moves is the per-task setup a host pays when
    # it re-discovers before every task. Measured in requests rather than
    # milliseconds, because requests are what a real network charges for.
    TASKS = 20

    def setup_requests(honour_ttl: bool) -> tuple[int, float]:
        host = build(fat=False, latency=LATENCY, cache=True)
        started = time.perf_counter()
        for _ in range(TASKS):
            if honour_ttl:
                host.discover_all()
                host.refresh_catalogue()
            else:
                # The naive host: re-fetch everything, every task.
                for binding in host.bindings.values():
                    binding.client.call("server/discover", use_cache=False)
                    binding.client.call("tools/list", use_cache=False)
        elapsed = (time.perf_counter() - started) * 1000.0
        issued = sum(b.client.stats.requests for b in host.bindings.values())
        return issued, elapsed / TASKS

    naive_reqs, naive_ms = setup_requests(False)
    ttl_reqs, ttl_ms = setup_requests(True)

    final = stages[-1][1]
    return {
        "results": rows,
        "derived": {
            "setupTasks": TASKS,
            "setupRequestsNaive": naive_reqs,
            "setupRequestsWithTtl": ttl_reqs,
            "setupMsNaive": round(naive_ms, 2),
            "setupMsWithTtl": round(ttl_ms, 2),
            "wallReductionPct": round(
                100 * (baseline_wall - final.wall_ms) / baseline_wall, 1),
            "tokenReductionPct": round(
                100 * (baseline_tok - final.total_tokens) / baseline_tok, 1),
            "costReductionPct": round(
                100 * (stages[0][1].cost_usd - final.cost_usd)
                / stages[0][1].cost_usd, 1),
            "iterationsBefore": len(stages[0][1].iterations),
            "iterationsAfter": len(final.iterations),
        },
    }


@scenario("serialisation")
def bench_serialisation() -> dict:
    """How much of a request is protocol envelope rather than payload."""
    from ..protocol import build_request_meta, encode

    caps = ClientCapabilities(elicitation={"form": {}, "url": {}},
                              extensions={tasks_ext.EXTENSION_ID: {}})
    meta = build_request_meta(caps)

    minimal = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
               "params": {"name": "assess_account_risk",
                          "arguments": {"accountId": "ACC-1042"}}}
    full = {**minimal, "params": {**minimal["params"], "_meta": meta}}

    payload_bytes = len(encode(minimal))
    total_bytes = len(encode(full))

    host = _wire()
    catalogue = host.catalogue()
    catalogue_bytes = len(encode(catalogue))

    return {
        "results": [
            {"label": "payload only", "bytes": payload_bytes},
            {"label": "with _meta envelope", "bytes": total_bytes},
            {"label": "tools/list catalogue", "bytes": catalogue_bytes,
             "tokens": estimate_tokens(encode(catalogue))},
        ],
        "derived": {
            "envelopeBytes": total_bytes - payload_bytes,
            "envelopeOverheadPct": round(
                100.0 * (total_bytes - payload_bytes) / payload_bytes, 1),
            "envelopeTokens": estimate_tokens(encode(meta)),
        },
    }


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


def machine() -> dict:
    return {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "processor": platform.processor() or platform.machine(),
    }


def run(names: list[str] | None = None) -> dict:
    chosen = names or list(SCENARIOS)
    out: dict[str, Any] = {"machine": machine(), "scenarios": {}}
    for name in chosen:
        fn = SCENARIOS.get(name)
        if fn is None:
            print(f"unknown scenario: {name}", file=sys.stderr)
            continue
        print(f"running {name} ...", file=sys.stderr, flush=True)
        started = time.perf_counter()
        out["scenarios"][name] = fn()
        out["scenarios"][name]["elapsedS"] = round(time.perf_counter() - started, 2)
    return out


def render(report: dict) -> str:
    lines = []
    m = report["machine"]
    lines.append(f"Machine: {m['machine']}, Python {m['python']}, {m['platform']}")
    lines.append("")
    for name, data in report["scenarios"].items():
        lines.append(f"== {name} ==")
        for row in data.get("results", []):
            label = row.get("label", "")
            if "meanMs" in row:
                lines.append(
                    f"  {label:<28} mean {row['meanMs']:>9.3f} ms   "
                    f"p50 {row['p50Ms']:>9.3f}   p95 {row['p95Ms']:>9.3f}   n={row['n']}"
                )
            else:
                # `steps` is a per-iteration array; it belongs in the JSON, not
                # in a terminal table.
                rest = "  ".join(f"{k}={v}" for k, v in row.items()
                                 if k not in ("label", "steps"))
                lines.append(f"  {label:<28} {rest}")
        derived = data.get("derived") or {}
        if derived:
            lines.append("  derived: " + "  ".join(f"{k}={v}" for k, v in derived.items()))
        lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Meridian measurement harness")
    ap.add_argument("scenarios", nargs="*", help="names to run (default: all)")
    ap.add_argument("--json", metavar="PATH", help="write the full report as JSON")
    ap.add_argument("--list", action="store_true", help="list scenario names")
    args = ap.parse_args(argv)

    if args.list:
        for name in SCENARIOS:
            print(name)
        return 0

    report = run(args.scenarios or None)
    print(render(report))
    if args.json:
        with open(args.json, "w") as fh:
            json.dump(report, fh, indent=2)
        print(f"wrote {args.json}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
