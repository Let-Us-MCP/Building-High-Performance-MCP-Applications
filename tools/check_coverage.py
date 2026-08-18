#!/usr/bin/env python3
"""Line coverage for meridian/, using only the standard library.

The book claims its companion code has no third-party runtime dependencies, and
pulling in coverage.py just to measure that felt like the wrong trade. `trace`
ships with Python and is enough for the question actually being asked: is there
a module in here that nothing exercises?

Reports per-module coverage and fails if any module falls below the floor.

    python3 tools/check_coverage.py
    python3 tools/check_coverage.py --floor 70
    python3 tools/check_coverage.py --show meridian/protocol/http.py
"""

from __future__ import annotations

import argparse
import sys
import threading
import trace
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PKG = ROOT / "meridian"

DEFAULT_FLOOR = 60

# These run in a child process, so no in-process tracer can see them. They are
# covered: `test_transports` drives a real stdio subprocess and asserts on what
# comes back. Reported for honesty, excluded from the floor because the number
# would be measuring the process boundary rather than the tests.
SUBPROCESS_ONLY = {"meridian/protocol/stdio.py", "meridian/serve.py"}

# Measuring the tests themselves tells you nothing, and __main__ blocks and the
# bench harness are run by hand rather than by the suite.
SKIP_PARTS = ("tests", "__init__.py", "bench")


def interesting(path: Path) -> bool:
    rel = path.relative_to(ROOT).parts
    return not any(part in SKIP_PARTS or part.startswith("_")
                   for part in rel[1:])


def run_suite() -> trace.CoverageResults:
    sys.path.insert(0, str(ROOT))
    tracer = trace.Trace(count=1, trace=0, ignoredirs=[sys.prefix, sys.exec_prefix])

    def discover_and_run():
        # Discovery has to happen inside the traced call. Importing a module
        # before the tracer starts leaves every class body, constant, and
        # decorator in it looking untouched, which understates small modules
        # badly: errors.py is almost entirely class definitions.
        suite = unittest.defaultTestLoader.discover(str(PKG / "tests"),
                                                    top_level_dir=str(ROOT))
        runner = unittest.TextTestRunner(stream=open("/dev/null", "w"), verbosity=0)
        return runner.run(suite)

    # The HTTP server answers on ThreadingHTTPServer workers, and `trace` only
    # follows the thread that called runfunc. Without this, the entire request
    # handler reads as dead code while the tests that exercise it pass.
    threading.settrace(tracer.globaltrace)
    try:
        tracer.runfunc(discover_and_run)
    finally:
        threading.settrace(None)
    return tracer.results()


def executable_lines(path: Path) -> set[int]:
    """Lines `trace` could plausibly count: not blank, comment, or docstring."""
    import tokenize

    with tokenize.open(path) as fh:
        src = fh.read()
    try:
        tree = compile(src, str(path), "exec", flags=0, dont_inherit=True)
    except SyntaxError:
        return set()

    lines: set[int] = set()
    stack = [tree]
    while stack:
        code = stack.pop()
        for entry in (code.co_lines() if hasattr(code, "co_lines") else []):
            line = entry[-1]          # (start, end, lineno) on 3.11+
            if line:
                lines.add(line)
        for const in code.co_consts:
            if hasattr(const, "co_lines") or hasattr(const, "co_consts"):
                stack.append(const)
    return lines


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--floor", type=int, default=DEFAULT_FLOOR)
    ap.add_argument("--show", metavar="PATH", help="list uncovered lines")
    args = ap.parse_args()

    results = run_suite()
    hit: dict[str, set[int]] = {}
    for (filename, lineno), count in results.counts.items():
        if count:
            hit.setdefault(filename, set()).add(lineno)

    rows = []
    for path in sorted(PKG.rglob("*.py")):
        if not interesting(path):
            continue
        total = executable_lines(path)
        if not total:
            continue
        covered = total & hit.get(str(path), set())
        pct = round(100 * len(covered) / len(total))
        rows.append((path.relative_to(ROOT), len(total), pct, sorted(total - covered)))

    if args.show:
        for rel, _, _, missing in rows:
            if str(rel) == args.show or rel.name == Path(args.show).name:
                print(f"{rel}: uncovered lines")
                print("  " + ", ".join(str(n) for n in missing))
                return 0
        print(f"no such module: {args.show}", file=sys.stderr)
        return 1

    print(f"{'module':<42}{'lines':>7}{'covered':>9}")
    print("-" * 58)
    low = []
    tot_lines = tot_cov = 0
    for rel, n, pct, _ in rows:
        tot_lines += n
        tot_cov += n * pct / 100
        flag = ""
        if str(rel) in SUBPROCESS_ONLY:
            flag = "  (subprocess)"
        elif pct < args.floor:
            flag = "  <-- under"
            low.append(str(rel))
        print(f"{str(rel):<42}{n:>7}{pct:>8}%{flag}")

    print("-" * 58)
    print(f"{'overall':<42}{tot_lines:>7}{round(100*tot_cov/tot_lines):>8}%")
    print(f"\n{len(low)} module(s) under the {args.floor}% floor")
    return 1 if low else 0


if __name__ == "__main__":
    raise SystemExit(main())
