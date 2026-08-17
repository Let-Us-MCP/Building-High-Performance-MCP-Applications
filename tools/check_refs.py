#!/usr/bin/env python3
"""Cross-reference checker for the book.

LaTeX reports an undefined reference as a warning buried in a 4,000-line log,
and then cheerfully typesets `??` into the finished page. That is a bad failure
mode for a book with several hundred internal links, so this checks directly.

Also flags labels nothing points at, which are usually a sign that a reference
got renamed and its target did not.

    python3 tools/check_refs.py
    python3 tools/check_refs.py --unused
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BOOK = ROOT / "book"

LABEL_RE = re.compile(r"\\label\{([^}]+)\}")

# The custom helpers from preamble.tex prefix their argument, so a `\figref{x}`
# is really a reference to `fig:x`. Track that mapping or every one looks broken.
REF_PATTERNS = [
    (re.compile(r"\\ref\{([^}]+)\}"), ""),
    (re.compile(r"\\pageref\{([^}]+)\}"), ""),
    (re.compile(r"\\autoref\{([^}]+)\}"), ""),
    (re.compile(r"\\figref\{([^}]+)\}"), "fig:"),
    (re.compile(r"\\chapref\{([^}]+)\}"), "ch:"),
    (re.compile(r"\\secref\{([^}]+)\}"), "sec:"),
    (re.compile(r"\\tabref\{([^}]+)\}"), "tab:"),
]

# `\bookfig{file}{caption}{label}` declares `fig:<label>` via its third argument.
BOOKFIG_RE = re.compile(
    r"\\bookfig(?:\[[^\]]*\])?\{[^{}]*\}\{(?:[^{}]|\{[^{}]*\})*\}\{([^{}]+)\}"
)


def tex_files() -> list[Path]:
    """Content files only.

    `preamble.tex` is skipped because it *defines* the reference helpers, and
    the `#1` inside those definitions is a macro parameter rather than a label.
    """
    out: list[Path] = []
    for sub in ("", "chapters", "appendices", "frontmatter"):
        out.extend(p for p in sorted((BOOK / sub).glob("*.tex"))
                   if p.name != "preamble.tex")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--unused", action="store_true",
                    help="also report labels nothing references")
    args = ap.parse_args()

    labels: dict[str, Path] = {}
    duplicates: list[tuple[str, Path, Path]] = []
    refs: dict[str, list[tuple[Path, int]]] = defaultdict(list)

    for path in tex_files():
        text = path.read_text(encoding="utf-8")

        for m in LABEL_RE.finditer(text):
            name = m.group(1)
            if name in labels:
                duplicates.append((name, labels[name], path))
            else:
                labels[name] = path

        for m in BOOKFIG_RE.finditer(text):
            name = "fig:" + m.group(1)
            if name in labels:
                duplicates.append((name, labels[name], path))
            else:
                labels[name] = path

        for pattern, prefix in REF_PATTERNS:
            for m in pattern.finditer(text):
                line = text.count("\n", 0, m.start()) + 1
                refs[prefix + m.group(1)].append((path, line))

    missing = {name: sites for name, sites in refs.items() if name not in labels}

    for name in sorted(missing):
        for path, line in missing[name]:
            print(f"{path.relative_to(ROOT)}:{line}: undefined reference "
                  f"{{{name}}}  (would typeset as ??)")

    for name, first, second in duplicates:
        print(f"{second.relative_to(ROOT)}: duplicate label {{{name}}} "
              f"(also in {first.relative_to(ROOT)})")

    if args.unused:
        for name in sorted(set(labels) - set(refs)):
            print(f"{labels[name].relative_to(ROOT)}: label {{{name}}} "
                  f"is never referenced")

    total_bad = sum(len(v) for v in missing.values()) + len(duplicates)
    print(f"\n{len(labels)} labels, {sum(len(v) for v in refs.values())} references, "
          f"{total_bad} problem(s)")
    return 1 if total_bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
