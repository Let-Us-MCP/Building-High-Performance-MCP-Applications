#!/usr/bin/env python3
"""Readability review, over and above the hard rules in lint_prose.py.

`lint_prose.py` enforces things that are simply wrong (em dashes, slop phrases,
duplicated sentences). This is the softer pass: it surfaces places the prose is
likely to be hard work for a reader, so a human can decide.

Reports, per file:

  * sentences over a length threshold, which are usually two sentences
  * paragraphs over a length threshold, which usually want a break or a list
  * runs of consecutive sentences opening with the same word, which read as a drone
  * the ratio of long to short sentences, since rhythm is what makes long-form
    technical prose readable

    python3 tools/review_prose.py
    python3 tools/review_prose.py --worst 15
"""

from __future__ import annotations

import argparse
import re
import statistics
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from lint_prose import ROOT, collect, strip_latex  # noqa: E402

LONG_SENTENCE = 45      # words
HUGE_SENTENCE = 60
LONG_PARAGRAPH = 145    # words
OPENER_RUN = 3          # consecutive sentences with the same first word

SENTENCE_RE = re.compile(r"[^.!?]+[.!?]")

# Tables and lists are not prose. Their cells run together into one enormous
# pseudo-sentence and drown every real finding, so they come out first.
_BLOCK_RE = re.compile(
    r"\\begin\{(table|tabular|tabularx|itemize|enumerate|description)\*?\}"
    r".*?\\end\{\1\*?\}",
    re.DOTALL)


# A removed code block leaves the prose before and after it adjacent, and with
# no full stop between them the sentence splitter reads them as one enormous
# sentence. Substituting a period keeps the boundary.
_CODE_RE = re.compile(
    r"\\begin\{(wire|http|py|ts|sh|plain|verbatim|lstlisting)\*?\}"
    r".*?\\end\{\1\*?\}",
    re.DOTALL)
_HEADING_RE = re.compile(r"\\(?:sub)*section\*?\{")


def strip_blocks(text: str) -> str:
    text = _CODE_RE.sub(" . ", text)
    # Headings are not part of the preceding sentence either.
    text = _HEADING_RE.sub(lambda m: ". " + m.group(0), text)
    for _ in range(4):                      # nested lists inside tables
        text, n = _BLOCK_RE.subn(" . ", text)
        if not n:
            break
    return text


def sentences(text: str) -> list[str]:
    return [" ".join(s.split()) for s in SENTENCE_RE.findall(text)
            if len(s.split()) > 1]


def words(text: str) -> list[str]:
    return re.findall(r"[A-Za-z][A-Za-z'-]*", text)


def paragraphs(prose: str) -> list[str]:
    return [" ".join(p.split()) for p in re.split(r"\n\s*\n", prose)
            if len(p.split()) > 3]


def review(path: Path, worst: int) -> dict:
    prose = strip_latex(strip_blocks(path.read_text(encoding="utf-8")))
    sents = sentences(prose)
    paras = paragraphs(prose)
    lengths = [len(words(s)) for s in sents]

    findings: list[tuple[int, str]] = []

    for s, n in zip(sents, lengths):
        if n >= HUGE_SENTENCE:
            findings.append((n, f"{n}-word sentence: {s[:110]}..."))
        elif n >= LONG_SENTENCE:
            findings.append((n, f"{n}-word sentence: {s[:90]}..."))

    for p in paras:
        n = len(words(p))
        if n >= LONG_PARAGRAPH:
            findings.append((n, f"{n}-word paragraph: {p[:90]}..."))

    # Repeated openers, which is what makes technical prose feel like a list.
    run, prev = 1, None
    for s in sents:
        first = (words(s) or [""])[0].lower()
        if first and first == prev:
            run += 1
            if run == OPENER_RUN:
                findings.append(
                    (100, f'{OPENER_RUN} sentences in a row open with "{first}"'))
        else:
            run, prev = 1, first

    findings.sort(reverse=True)
    return {
        "path": path,
        "sentences": len(sents),
        "words": sum(lengths),
        "mean": statistics.fmean(lengths) if lengths else 0,
        "median": statistics.median(lengths) if lengths else 0,
        "short_pct": 100 * sum(1 for n in lengths if n <= 12) / len(lengths)
                     if lengths else 0,
        "long_pct": 100 * sum(1 for n in lengths if n >= LONG_SENTENCE) / len(lengths)
                    if lengths else 0,
        "findings": findings[:worst],
        "n_findings": len(findings),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("paths", nargs="*")
    ap.add_argument("--worst", type=int, default=6,
                    help="findings to show per file")
    ap.add_argument("--summary", action="store_true",
                    help="table only, no findings")
    args = ap.parse_args()

    files = collect(args.paths)
    reports = [review(p, args.worst) for p in files]

    print(f"{'file':<34}{'sents':>7}{'mean':>7}{'med':>6}"
          f"{'short%':>8}{'long%':>7}{'flags':>7}")
    print("-" * 76)
    for r in reports:
        rel = r["path"].relative_to(ROOT)
        print(f"{str(rel.name):<34}{r['sentences']:>7}{r['mean']:>7.1f}"
              f"{r['median']:>6.0f}{r['short_pct']:>8.0f}{r['long_pct']:>7.1f}"
              f"{r['n_findings']:>7}")

    allsents = sum(r["sentences"] for r in reports)
    allwords = sum(r["words"] for r in reports)
    print("-" * 76)
    print(f"{'TOTAL':<34}{allsents:>7}{allwords / max(allsents, 1):>7.1f}")
    print(f"\n~{allwords:,} words of prose across {len(reports)} files")

    if not args.summary:
        for r in reports:
            if not r["findings"]:
                continue
            print(f"\n=== {r['path'].relative_to(ROOT)} "
                  f"({r['n_findings']} flags) ===")
            for _, msg in r["findings"]:
                print("  ", msg)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
