#!/usr/bin/env python3
"""Find LinkedIn cadence: prose broken into punchy fragments instead of argument.

A one-sentence paragraph is a fine thing to use occasionally, for emphasis. A
book made of them reads like a slide deck, or a LinkedIn post: every idea gets a
line, no idea gets developed, and the reader is asked to be impressed rather than
convinced.

This counts, per file, the share of body paragraphs that are one sentence long
and the share under 25 words. Genuinely short paragraphs that are doing a real
job are excluded: list items, table cells, code, headings, captions, boxes, and
the deliberate one-liners inside \\keyidea.

    python3 tools/check_cadence.py
    python3 tools/check_cadence.py --show book/chapters/ch05.tex
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BOOK = ROOT / "book"

# Budgets. Both are shares of body paragraphs, in percent.
ONE_SENTENCE_BUDGET = 35
SHORT_BUDGET = 52

ENV_STRIP = re.compile(
    r"\\begin\{(py|wire|http|ts|sh|plain|tabular|tabularx|table|figure|"
    r"itemize|enumerate|description|measurebox|legacybox|dangerbox|notebox|"
    r"lstlisting)\*?\}.*?\\end\{\1\*?\}",
    re.DOTALL)
CMD_LINE = re.compile(r"^\s*\\(chapter|section|subsection|subsubsection|label|"
                      r"caption|epigraph|bookfig|keyidea|input|begin|end|item|"
                      r"toprule|midrule|bottomrule|vspace|newpage|clearpage).*$",
                      re.MULTILINE)
INLINE = re.compile(r"\\(texttt|emph|textbf|ref|secref|chapref|figref)\{([^{}]*)\}")


def strip_braced(s: str, command: str) -> str:
    """Remove `\\command{..}{..}`, which CMD_LINE cannot do because the
    arguments routinely wrap across lines."""
    out = []
    i = 0
    while True:
        j = s.find(command, i)
        if j < 0:
            out.append(s[i:])
            return "".join(out)
        out.append(s[i:j])
        k = j + len(command)
        while k < len(s) and s[k] in " \n":
            k += 1
        while k < len(s) and s[k] == "{":
            depth = 0
            while k < len(s):
                if s[k] == "{":
                    depth += 1
                elif s[k] == "}":
                    depth -= 1
                k += 1
                if depth == 0:
                    break
        i = k


def body_paragraphs(raw: str) -> list[str]:
    raw = strip_braced(raw, "\\epigraph")
    raw = strip_braced(raw, "\\keyidea")
    raw = strip_braced(raw, "\\bookfig")
    s = ENV_STRIP.sub("\n\n", raw)
    s = re.sub(r"%.*$", "", s, flags=re.MULTILINE)
    s = CMD_LINE.sub("\n\n", s)
    s = INLINE.sub(r"\2", s)
    s = s.replace("\\,", " ").replace("\\%", "%").replace("\\$", "$")
    s = re.sub(r"\\[a-zA-Z]+\*?", " ", s)
    s = re.sub(r"[{}]", " ", s)

    out = []
    for para in re.split(r"\n\s*\n", s):
        text = " ".join(para.split())
        if len(text.split()) >= 5:
            out.append(text)
    return out


def sentence_count(para: str) -> int:
    # Abbreviations that should not end a sentence.
    guarded = re.sub(r"\b(e\.g|i\.e|etc|vs|Dr|Mr|Ms|St|approx|Fig|No)\.", r"\1<DOT>",
                     para)
    guarded = re.sub(r"(\d)\.(\d)", r"\1<DOT>\2", guarded)
    parts = [p for p in re.split(r"(?<=[.!?])\s+", guarded) if p.strip()]
    return max(1, len(parts))


def measure(path: Path) -> dict:
    paras = body_paragraphs(path.read_text(encoding="utf-8"))
    if not paras:
        return {}
    # A short paragraph ending in a colon is introducing the listing or table
    # underneath it. That is ordinary technical writing, not a fragment used as
    # a drumroll, so it does not count against either budget.
    paras = [p for p in paras if not p.rstrip().endswith(":")]
    if not paras:
        return {}
    one = [p for p in paras if sentence_count(p) == 1]
    short = [p for p in paras if len(p.split()) <= 25]
    return {
        "paras": len(paras),
        "onePct": round(100 * len(one) / len(paras)),
        "shortPct": round(100 * len(short) / len(paras)),
        "worst": sorted(one, key=lambda p: len(p.split()))[:6],
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("paths", nargs="*")
    ap.add_argument("--show", action="store_true")
    ap.add_argument("--budget", type=int, default=ONE_SENTENCE_BUDGET)
    args = ap.parse_args()

    # Chapters and the preface, which is where running argument lives. The
    # appendices are checklists, reference tables, and a bibliography, and
    # "how to read this book" is navigation; in all of those a terse line is
    # the correct form and this rule would be measuring the wrong thing.
    paths = [Path(p) for p in args.paths] or sorted(
        list((BOOK / "chapters").glob("*.tex"))
        + [BOOK / "frontmatter" / "preface.tex"])

    print(f"{'file':<24}{'paras':>7}{'1-sent':>9}{'<=25w':>8}")
    print("-" * 52)
    over = []
    total = one_total = short_total = 0
    for path in paths:
        m = measure(path)
        if not m:
            continue
        total += m["paras"]
        one_total += m["onePct"] * m["paras"] / 100
        short_total += m["shortPct"] * m["paras"] / 100
        flag = ""
        if m["onePct"] > args.budget or m["shortPct"] > SHORT_BUDGET:
            flag = "  <-- over"
            over.append(path.name)
        print(f"{path.name:<24}{m['paras']:>7}{m['onePct']:>8}%{m['shortPct']:>7}%{flag}")
        if args.show and flag:
            for w in m["worst"]:
                print(f"        {w[:96]}")

    print("-" * 52)
    print(f"{'overall':<24}{total:>7}{round(100*one_total/total):>8}%"
          f"{round(100*short_total/total):>7}%")
    print(f"\n{len(over)} file(s) over budget "
          f"({args.budget}% one-sentence, {SHORT_BUDGET}% short)")
    return 1 if over else 0


if __name__ == "__main__":
    raise SystemExit(main())
