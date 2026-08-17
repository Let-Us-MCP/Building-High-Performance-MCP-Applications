#!/usr/bin/env python3
"""Verify the numbers printed in the book match the committed benchmark run.

The book's whole premise is that its measurements are real. That promise decays
the moment somebody reruns `make bench` and the prose keeps quoting the old
figures, which is exactly what happened once during writing and is why this
exists.

`meridian/bench/results.json` is the canonical run: the one the prose quotes.
Timings jitter between runs, so rerunning the harness is expected to move the
numbers slightly. When it does, update the prose and commit both together.

    python3 tools/check_numbers.py
    python3 tools/check_numbers.py --show    # print the canonical values
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RESULTS = ROOT / "meridian" / "bench" / "results.json"
BOOK = ROOT / "book"


def canonical() -> dict[str, float]:
    d = json.loads(RESULTS.read_text())
    sc = d["scenarios"]
    loop = sc["loop"]["results"][0]
    tr = {r["label"]: r for r in sc["transport"]["results"]}
    cat = {r["label"]: r for r in sc["catalogue"]["results"]}
    fan = sc["fanout"]["results"]
    opt = {r["label"]: r for r in sc["optimization"]["results"]}
    ser = {r["label"]: r for r in sc["serialisation"]["results"]}

    return {
        "loop.wallMs": loop["wallMs"],
        "loop.modelMs": loop["modelMs"],
        "loop.transportMs": loop["transportMs"],
        "loop.modelSharePct": loop["modelSharePct"],
        "loop.totalTokens": loop["totalTokens"],
        "transport.inproc": tr["in-process"]["meanMs"],
        "transport.stdio": tr["stdio"]["meanMs"],
        "transport.http": tr["streamable http"]["meanMs"],
        "coldstart.ms": sc["coldstart"]["derived"]["coldStartMs"],
        "coldstart.calls": sc["coldstart"]["derived"]["callsToAmortiseColdStart"],
        "catalogue.slimTokens": cat["slim catalogue"]["tokens"],
        "catalogue.fatTokens": cat["fat catalogue"]["tokens"],
        "catalogue.extraPerTurn": sc["catalogue"]["derived"]["extraTokensPerTurn"],
        "cache.hitRatePct": sc["cache"]["derived"]["hitRatePct"],
        "cache.speedup": sc["cache"]["derived"]["speedup"],
        "cache.avoided": sc["cache"]["derived"]["requestsAvoided"],
        "fanout.40.serial": fan[2]["serialMs"],
        "fanout.40.parallel": fan[2]["parallelMs"],
        "mrtr.extraTransportMs": sc["mrtr"]["derived"]["extraTransportMs"],
        "opt.baselineWall": opt["baseline"]["wallMs"],
        "opt.finalWall": opt["fan out in parallel"]["wallMs"],
        "opt.baselineTokens": opt["baseline"]["tokens"],
        "opt.finalTokens": opt["fan out in parallel"]["tokens"],
        "opt.setupNaive": sc["optimization"]["derived"]["setupRequestsNaive"],
        "opt.setupTtl": sc["optimization"]["derived"]["setupRequestsWithTtl"],
        "serial.envelopeBytes": sc["serialisation"]["derived"]["envelopeBytes"],
        "serial.catalogueBytes": ser["tools/list catalogue"]["bytes"],
    }


def latex_number(value: float) -> list[str]:
    """The forms a number may legitimately take in the LaTeX source.

    Prose rounds. "roughly 750 warm calls" is the same claim as 746, and a table
    that prints 1,753 is quoting 1752.9. Both are accepted; a number that has
    genuinely moved will match none of these forms.
    """
    forms: set[str] = set()

    def add_int(n: int) -> None:
        forms.add(str(n))
        if abs(n) >= 1000:
            forms.add(f"{n:,}".replace(",", "{,}"))
            forms.add(f"{n:,}")

    add_int(round(value))
    if not float(value).is_integer():
        for places in (1, 2, 3):
            forms.add(f"{value:.{places}f}")
            if value >= 1000:
                forms.add(f"{value:,.{places}f}".replace(",", "{,}"))
        forms.add(f"{value:g}")

    # Prose often rounds to a tidy figure: 746 -> 750, 1753 -> 1750.
    magnitude = 10 if abs(value) < 1000 else 50
    add_int(int(round(value / magnitude) * magnitude))
    return sorted(forms)


def book_text() -> str:
    parts = []
    for sub in ("chapters", "appendices", "frontmatter"):
        for p in sorted((BOOK / sub).glob("*.tex")):
            parts.append(p.read_text(encoding="utf-8"))
    return "\n".join(parts)


# Values that legitimately appear nowhere in the prose (used only by figures),
# or that are too generic to search for without false positives.
FIGURE_ONLY = {"loop.modelMs", "transport.inproc", "serial.catalogueBytes"}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--show", action="store_true")
    args = ap.parse_args()

    if not RESULTS.exists():
        print("meridian/bench/results.json missing; run `make bench`", file=sys.stderr)
        return 1

    values = canonical()
    if args.show:
        for k, v in values.items():
            print(f"{k:<28} {v}")
        return 0

    text = book_text()
    missing = []
    for key, value in values.items():
        if key in FIGURE_ONLY:
            continue
        if not any(f in text for f in latex_number(value)):
            missing.append((key, value, latex_number(value)))

    for key, value, forms in missing:
        print(f"STALE: {key} = {value} appears nowhere in the book "
              f"(looked for {', '.join(forms)})")

    print(f"\n{len(values)} canonical values, "
          f"{len(values) - len(FIGURE_ONLY)} checked, {len(missing)} stale")
    if missing:
        print("\nThe benchmarks moved and the prose did not. Update the prose, "
              "or re-pin results.json.")
    return 1 if missing else 0


if __name__ == "__main__":
    raise SystemExit(main())
