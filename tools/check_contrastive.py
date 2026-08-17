#!/usr/bin/env python3
"""Find contrastive constructions, which are a tic rather than a style.

"X is not Y. It is Z." is a fine sentence. Forty of them in a chapter is a
mannerism, and it makes prose feel like it is arguing with somebody who is not
in the room. Straight declarative writing says what the thing IS and moves on.

    Tic:      "The need for speed is not a strategy. It is an appetite."
    Straight: "The need for speed is an appetite."

    Tic:      "That is not sloppiness. It is the binding doing its job."
    Straight: "That is the binding doing its job."

Some contrast is load-bearing: correcting a belief a reader actually holds is
the whole point of a paragraph sometimes. The aim is a budget, not zero.

    python3 tools/check_contrastive.py
    python3 tools/check_contrastive.py --show ch03
    python3 tools/check_contrastive.py --budget 6
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from lint_prose import ROOT, collect, strip_latex  # noqa: E402

# Ordered roughly by how strongly each signals the tic.
PATTERNS = [
    ("is-not-it-is",
     r"\b(?:is|was|are|were)\s+not\b[^.!?]{0,80}[.!?]\s+(?:It|That|This|They)\s+(?:is|was|are|were)\b"),
    ("not-X-but-Y",
     r"\bnot\s+(?:just|only|merely|simply)?\s*[^.,;:]{2,60}?,?\s+but\b"),
    ("it-is-not-it-is",
     r"\b(?:It|That|This)\s+(?:is|was)\s+not\b[^.!?]{0,60},\s*(?:it|that|this)\s+(?:is|was)\b"),
    ("X-not-Y-comma",
     r",\s+not\s+[a-z][^.,;:!?]{2,40}[.;]"),
    ("rather-than",
     r"\brather than\b"),
    ("instead-of",
     r"\binstead of\b"),
    ("the-point-is-not",
     r"\bthe (?:point|question|issue|problem|goal) is not\b"),
    ("not-because-but-because",
     r"\bnot because\b[^.!?]{0,80}?\bbut because\b"),
    ("less-X-more-Y",
     r"\bless (?:a|an|about)\b[^.!?]{0,50}?\bmore (?:a|an|about)\b"),
    ("what-changed-is-not",
     r"\bwhat (?:changed|matters|counts) is not\b"),
]

COMPILED = [(name, re.compile(p, re.IGNORECASE)) for name, p in PATTERNS]

DEFAULT_BUDGET = 8   # per file, across all patterns


def scan(path: Path) -> list[tuple[str, int, str]]:
    prose = strip_latex(path.read_text(encoding="utf-8"))
    hits: list[tuple[str, int, str]] = []
    seen_spans: list[tuple[int, int]] = []
    for name, pattern in COMPILED:
        for m in pattern.finditer(prose):
            # Do not double-count overlapping constructions.
            if any(not (m.end() <= s or m.start() >= e) for s, e in seen_spans):
                continue
            seen_spans.append((m.start(), m.end()))
            line = prose.count("\n", 0, m.start()) + 1
            snippet = " ".join(prose[max(0, m.start() - 40):m.end() + 40].split())
            hits.append((name, line, snippet))
    hits.sort(key=lambda h: h[1])
    return hits


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("paths", nargs="*")
    ap.add_argument("--budget", type=int, default=DEFAULT_BUDGET)
    ap.add_argument("--show", action="store_true", help="list every hit")
    args = ap.parse_args()

    files = collect(args.paths)
    over = 0
    total = 0

    print(f"{'file':<26}{'hits':>6}{'per 1k words':>14}   {'top patterns'}")
    print("-" * 84)

    for path in files:
        prose = strip_latex(path.read_text(encoding="utf-8"))
        words = len(re.findall(r"[A-Za-z][A-Za-z'-]+", prose))
        hits = scan(path)
        total += len(hits)
        density = 1000 * len(hits) / max(words, 1)
        counts: dict[str, int] = {}
        for name, _, _ in hits:
            counts[name] = counts.get(name, 0) + 1
        top = ", ".join(f"{n}x{c}" for n, c in
                        sorted(counts.items(), key=lambda kv: -kv[1])[:3])
        flag = "  <-- over" if len(hits) > args.budget else ""
        if len(hits) > args.budget:
            over += 1
        print(f"{path.name:<26}{len(hits):>6}{density:>14.1f}   {top}{flag}")

        if args.show and hits:
            for name, line, snippet in hits:
                print(f"      {line:>5}  [{name}] ...{snippet}...")

    print("-" * 84)
    print(f"{total} contrastive constructions, {over} file(s) over a budget of "
          f"{args.budget}")
    return 1 if over else 0


if __name__ == "__main__":
    raise SystemExit(main())
