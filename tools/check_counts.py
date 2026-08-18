#!/usr/bin/env python3
"""Verify counts the book asserts about its own companion code.

The book says things like "Meridian has 134 tests" in a dozen places. Every one
of those is a fact with a shelf life, and adding a test file is exactly the kind
of change nobody thinks to grep for. This found eleven stale claims the first
time it ran.

Counts checked:

    tests       collected by unittest discovery, not parsed out of source
    test files  files matching meridian/tests/test_*.py
    servers     modules in meridian/servers that build a server

    python3 tools/check_counts.py
    python3 tools/check_counts.py --show
"""

from __future__ import annotations

import argparse
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BOOK = ROOT / "book"


def count_tests() -> int:
    """Load the suite and count leaf cases.

    Discovery rather than a regex over `def test_`, because a regex counts
    helpers named test_something and misses cases generated at class-build
    time.
    """
    sys.path.insert(0, str(ROOT))
    suite = unittest.defaultTestLoader.discover(
        str(ROOT / "meridian" / "tests"), top_level_dir=str(ROOT))

    def leaves(s) -> int:
        if isinstance(s, unittest.TestSuite):
            return sum(leaves(x) for x in s)
        return 1

    return leaves(suite)


def counts() -> dict[str, int]:
    return {
        "tests": count_tests(),
        "testFiles": len(list((ROOT / "meridian" / "tests").glob("test_*.py"))),
    }


def book_text() -> str:
    parts = []
    for sub in ("chapters", "appendices", "frontmatter"):
        for p in sorted((BOOK / sub).glob("*.tex")):
            parts.append(p.read_text(encoding="utf-8"))
    # Every prose file that states a count, not just the book. VERIFICATION.md
    # went eleven commits stale because it sat one directory outside this list.
    for extra in [ROOT / "README.md", ROOT / "CLAUDE.md",
                  *sorted((ROOT / "meridian").rglob("*.md"))]:
        if extra.exists():
            parts.append(extra.read_text(encoding="utf-8"))
    return "\n".join(parts)


# Any number in this range that appears next to the word "tests" is claiming to
# be the test count. Anything else is a different fact that happens to be a
# number, so the pattern has to be anchored on the word.
#
# Except when it is quoted. Prose that discusses a wrong count — this file's own
# docstring, the notes recording that the book once said "134 tests" — is
# reporting the string rather than claiming it, and a checker that cannot tell
# the difference makes writing about its own findings impossible.
TEST_CLAIM = re.compile(
    r"""(?<!["'`])\b(\d{2,4})\s*(?:\\,)?\s*tests\b(?!["'`])""", re.IGNORECASE)
FILES_CLAIM = re.compile(r"tests across (\w+) files", re.IGNORECASE)

WORDS = {"two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
         "eight": 8, "nine": 9, "ten": 10}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--show", action="store_true")
    args = ap.parse_args()

    actual = counts()
    if args.show:
        for k, v in actual.items():
            print(f"{k:<12} {v}")
        return 0

    text = book_text()
    problems = []

    claimed = {int(m.group(1)) for m in TEST_CLAIM.finditer(text)}
    for n in sorted(claimed):
        if n != actual["tests"]:
            problems.append(f"the book claims {n} tests; there are {actual['tests']}")

    for m in FILES_CLAIM.finditer(text):
        word = m.group(1).lower()
        n = WORDS.get(word, int(word) if word.isdigit() else None)
        if n is not None and n != actual["testFiles"]:
            problems.append(
                f"the book says tests span {word} files; there are "
                f"{actual['testFiles']}")

    for p in problems:
        print(f"STALE: {p}")
    print(f"\n{len(actual)} counts, {len(claimed)} distinct test-count claims, "
          f"{len(problems)} stale")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
