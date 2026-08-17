#!/usr/bin/env python3
"""Whitespace-insensitive search and replace for LaTeX prose, with rewrapping.

Editing wrapped prose by exact string match is miserable: the source is hard
wrapped at 79 columns, so a phrase you want to change is usually split across a
line break in a way you have to guess. This matches on normalised whitespace and
rewraps the result, so an edit can be written the way the sentence reads.

Used as a library by the editing passes:

    from rewrite import apply_edits
    apply_edits("book/chapters/ch05.tex", [(old, new), ...])
"""

from __future__ import annotations

import re
import textwrap
from pathlib import Path

WIDTH = 79


def _flexible(pattern: str) -> re.Pattern:
    """A regex matching `pattern` with any run of whitespace between words."""
    return re.compile(r"\s+".join(re.escape(w) for w in pattern.split()))


def apply_edits(path: str | Path, edits: list[tuple[str, str]],
                *, wrap: bool = True) -> tuple[int, list[str]]:
    """Apply (old, new) pairs. Returns (applied, list of unmatched olds)."""
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    applied, missed = 0, []

    for old, new in edits:
        rx = _flexible(old)
        m = rx.search(text)
        if not m:
            missed.append(old[:70])
            continue
        replacement = " ".join(new.split())
        if wrap:
            # Rewrap starting from the column the match began at, so the
            # paragraph keeps its shape.
            line_start = text.rfind("\n", 0, m.start()) + 1
            indent = len(text[line_start:m.start()])
            wrapped = textwrap.fill(
                replacement, width=WIDTH,
                initial_indent="", subsequent_indent="",
                break_long_words=False, break_on_hyphens=False)
            if indent and "\n" in wrapped:
                first, rest = wrapped.split("\n", 1)
                wrapped = first + "\n" + rest
            replacement = wrapped
        text = text[:m.start()] + replacement + text[m.end():]
        applied += 1

    p.write_text(text, encoding="utf-8")
    return applied, missed


def rewrap_paragraphs(path: str | Path) -> None:
    """Rewrap prose paragraphs to WIDTH, leaving environments untouched."""
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    out, i = [], 0
    env = re.compile(r"\\begin\{(\w+\*?)\}.*?\\end\{\1\}", re.DOTALL)

    for m in env.finditer(text):
        out.append(_rewrap_prose(text[i:m.start()]))
        out.append(m.group(0))
        i = m.end()
    out.append(_rewrap_prose(text[i:]))
    p.write_text("".join(out), encoding="utf-8")


def _rewrap_prose(chunk: str) -> str:
    paras = re.split(r"(\n\s*\n)", chunk)
    result = []
    for part in paras:
        if part.strip() == "" or part.lstrip().startswith("\\") or "&" in part:
            result.append(part)
            continue
        if len(part) < WIDTH:
            result.append(part)
            continue
        result.append(textwrap.fill(" ".join(part.split()), width=WIDTH,
                                    break_long_words=False,
                                    break_on_hyphens=False))
    return "".join(result)


if __name__ == "__main__":
    print(__doc__)
