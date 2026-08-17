#!/usr/bin/env python3
"""Audit the book for the three failures that make technical prose hard to read.

1. JUMPY. A term is used before it is defined, or a concept is introduced and
   then deferred ("Chapter 8 covers this properly"). HPBN never does this: it
   defines propagation delay, transmission delay, processing delay, and queuing
   delay before it uses any of them.

2. UNDEFINED. A term is used and never defined anywhere in the book.

3. SWEEPING. An absolute claim with nothing behind it. "Always", "never",
   "everyone", "the single most", "obviously". Some are earned. Most are lazy.

Reads chapters in reading order, so "before" means what a reader experiences.

    python3 tools/audit_style.py
    python3 tools/audit_style.py --terms      # term-definition report only
    python3 tools/audit_style.py --sweeping   # absolute claims only
    python3 tools/audit_style.py ch01         # one chapter
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from lint_prose import ROOT, strip_latex  # noqa: E402

BOOK = ROOT / "book"

# Reading order. Front matter first, because a reader meets it first.
READING_ORDER = (
    [BOOK / "frontmatter" / n for n in ("preface.tex", "howtoread.tex")]
    + [BOOK / "chapters" / f"ch{i:02d}.tex" for i in range(1, 21)]
    + [BOOK / "chapters" / "epilogue.tex"]
    + [BOOK / "appendices" / f"app{c}.tex" for c in "ABCDE"]
)

# Terms a reader cannot be assumed to know. Each must be defined before use.
TERMS = {
    "agent loop": r"\bagent loop\b",
    "elicitation": r"\belicitation\b",
    "MRTR": r"\bMRTR\b",
    "host": r"\bthe host\b",
    "tool call": r"\btool call\b",
    "time to first token": r"\btime to first token\b|\bTTFT\b",
    "model turn": r"\bmodel turn\b",
    "round trip": r"\bround trip\b",
    "prompt cache": r"\bprompt cach",
    "prompt injection": r"\bprompt injection\b",
    "tool catalogue": r"\b(?:tool )?catalogue\b",
    "structuredContent": r"structuredContent",
    "resultType": r"resultType",
    "requestState": r"requestState",
    "ttlMs": r"ttlMs",
    "cacheScope": r"cacheScope",
    "fan-out": r"\bfan[- ]out\b",
    "idempotent": r"\bidempoten",
    "confused deputy": r"\bconfused deputy\b",
    "stateless": r"\bstateless\b",
    "dual-era": r"\bdual-era\b",
    "Streamable HTTP": r"\bStreamable HTTP\b",
    "stdio": r"\bstdio\b",
    "task handle": r"\btask (?:handle|id)\b",
    "consent gate": r"\bconsent gate\b",
    "audit log": r"\baudit log\b",
    "eval": r"\bevals?\b",
    "circuit breaker": r"\bcircuit breaker\b",
    "trifecta": r"\btrifecta\b",
}

# Patterns that constitute defining a term rather than merely using it.
DEFINITION_CUES = [
    r"\bis\b", r"\bare\b", r"\bmeans\b", r"\bcalled\b", r"\bnamely\b",
    r"\brefers to\b", r"\bdefine[sd]?\b", r"\bthat is\b", r"\bwhich is\b",
    r"\bwhat .{0,20}(?:is|are)\b", r":",
]

# Deferral: introducing a thing and pushing the explanation to later.
DEFERRALS = [
    (re.compile(r"Chapter~\\ref\{[^}]+\} (?:covers|explains|treats|does|has|builds|"
                r"spends|is about|goes|will)", re.I), "defers to a later chapter"),
    (re.compile(r"\bfor now,? (?:only|just|the)\b", re.I), "'for now' deferral"),
    (re.compile(r"\bwe (?:will|shall) get to\b", re.I), "'we will get to'"),
    (re.compile(r"\bmore on (?:this|that) (?:in|later)\b", re.I), "'more on this later'"),
    (re.compile(r"\b(?:covered|covers) (?:properly|in full|in depth) in\b", re.I),
     "'covered properly in'"),
    (re.compile(r"\bthe mechanism (?:properly|in full)\b", re.I), "mechanism deferred"),
]

# Absolute claims. Each needs evidence in the same neighbourhood or it is noise.
SWEEPING = [
    (r"\balways\b", "always"),
    (r"\bnever\b", "never"),
    (r"\beverybody\b|\beveryone\b", "everyone"),
    (r"\bnobody\b|\bno one\b", "nobody"),
    (r"\bevery (?:system|team|company|deployment|host|server|time)\b", "every X"),
    (r"\bthe single most\b", "the single most"),
    (r"\bthe most (?:important|common|useful|valuable)\b", "the most X"),
    (r"\bobviously\b|\bclearly\b|\bof course\b", "obviously"),
    (r"\ball (?:of )?(?:the )?(?:systems|teams|servers|hosts|clients)\b", "all X"),
    (r"\bany(?:one|body) (?:who|that)\b", "anyone who"),
    (r"\bcompletely\b|\bentirely\b|\btotally\b", "completely"),
    (r"\bnothing\b(?! (?:else|more|in|at all,))", "nothing"),
]

# Evidence within this many characters excuses an absolute claim.
EVIDENCE_WINDOW = 400
EVIDENCE = re.compile(
    r"\d+(?:[.,]\d+)?\s*(?:ms|\\,ms|%|\\,\\%|tokens|bytes|requests|x|\$)"
    r"|measurebox|\\ref\{tab:|specification (?:says|requires|is)|MUST|SHOULD",
    re.I)


def prose_of(path: Path) -> str:
    """Prose as a reader meets it.

    `strip_latex` blanks the argument of \\texttt and friends, which is right
    for the slop linter and wrong here: `ttlMs` is a term a reader has to learn,
    and it is always set in \\texttt. So unwrap those first.
    """
    raw = path.read_text(encoding="utf-8")
    raw = re.sub(r"\\(?:texttt|code|emph|textbf|textit)\{([^{}]*)\}", r"\1", raw)
    return strip_latex(raw)


def audit_terms(files: list[Path]) -> list[str]:
    """Find the first use of each term and judge whether it was explained."""
    findings = []
    first_seen: dict[str, tuple[Path, int, str]] = {}

    for path in files:
        prose = prose_of(path)
        for term, pattern in TERMS.items():
            if term in first_seen:
                continue
            m = re.search(pattern, prose, re.I)
            if not m:
                continue
            # The sentence containing the first use, plus what precedes it.
            start = max(0, m.start() - 320)
            context = " ".join(prose[start:m.end() + 220].split())
            first_seen[term] = (path, m.start(), context)

    for term, (path, _pos, context) in sorted(first_seen.items()):
        defined = any(re.search(cue, context, re.I) for cue in DEFINITION_CUES)
        if not defined:
            findings.append(
                f"{path.name}: '{term}' first used with no definitional cue\n"
                f"      ...{context[:150]}...")

    for term in TERMS:
        if term not in first_seen:
            findings.append(f"(book): '{term}' never appears")
    return findings


def audit_deferrals(files: list[Path]) -> list[str]:
    findings = []
    for path in files:
        prose = prose_of(path)
        for pattern, label in DEFERRALS:
            for m in pattern.finditer(prose):
                line = prose.count("\n", 0, m.start()) + 1
                snippet = " ".join(prose[max(0, m.start() - 90):m.end() + 90].split())
                findings.append(f"{path.name}:{line}: {label}\n      ...{snippet}...")
    return findings


def audit_sweeping(files: list[Path]) -> list[str]:
    findings = []
    for path in files:
        prose = prose_of(path)
        for pattern, label in SWEEPING:
            for m in re.finditer(pattern, prose, re.I):
                lo = max(0, m.start() - EVIDENCE_WINDOW)
                hi = min(len(prose), m.end() + EVIDENCE_WINDOW)
                if EVIDENCE.search(prose[lo:hi]):
                    continue                       # backed by a number nearby
                line = prose.count("\n", 0, m.start()) + 1
                snippet = " ".join(prose[max(0, m.start() - 80):m.end() + 100].split())
                findings.append(f"{path.name}:{line}: unsupported '{label}'\n"
                                f"      ...{snippet}...")
    return findings


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("only", nargs="*", help="chapter stems, e.g. ch01")
    ap.add_argument("--terms", action="store_true")
    ap.add_argument("--deferrals", action="store_true")
    ap.add_argument("--sweeping", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    files = [p for p in READING_ORDER if p.exists()]
    scope = files
    if args.only:
        scope = [p for p in files if p.stem in args.only]

    show_all = not (args.terms or args.deferrals or args.sweeping)
    total = 0

    if show_all or args.terms:
        f = audit_terms(files if not args.only else scope)
        print(f"\n### JUMPY: terms used without explanation ({len(f)})\n")
        for x in (f[:args.limit] if args.limit else f):
            print("  " + x)
        total += len(f)

    if show_all or args.deferrals:
        f = audit_deferrals(scope)
        print(f"\n### JUMPY: concepts deferred to later chapters ({len(f)})\n")
        for x in (f[:args.limit] if args.limit else f):
            print("  " + x)
        total += len(f)

    if show_all or args.sweeping:
        f = audit_sweeping(scope)
        print(f"\n### SWEEPING: absolute claims with no nearby evidence ({len(f)})\n")
        for x in (f[:args.limit] if args.limit else f):
            print("  " + x)
        total += len(f)

    print(f"\n{total} findings")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
