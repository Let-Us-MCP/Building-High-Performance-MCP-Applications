#!/usr/bin/env python3
"""Prose linter for the book.

Enforces the house rules from `desc`:

  1. No em dashes. Not one.
  2. No AI slop: a banned-phrase list of the tics that make text read as generated.
  3. No saying the same thing twice: near-duplicate sentence detection, within a
     chapter and across the whole book.
  4. No epigraphs. Chapters open on their argument.

Usage:
    python3 tools/lint_prose.py                 # lint everything under book/
    python3 tools/lint_prose.py book/chapters/ch05.tex
    python3 tools/lint_prose.py --quiet         # exit code only

Exit code is 1 if any error-level finding survives, 0 otherwise.
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------------------
# Rule 1: dashes
# ---------------------------------------------------------------------------

# Em dash proper, plus the LaTeX ligature that produces one, plus the
# "spaced en dash" habit that reads as an em dash on the page.
DASH_PATTERNS = [
    ("em dash (U+2014)", re.compile(r"\u2014")),
    ("LaTeX em dash (---)", re.compile(r"---")),
    ("spaced en dash used as an em dash", re.compile(r"(?<=\w)\s+\u2013\s+(?=\w)")),
    ("spaced LaTeX en dash used as an em dash", re.compile(r"(?<=\w)\s+--\s+(?=\w)")),
]

# ---------------------------------------------------------------------------
# Rule 2: slop
# ---------------------------------------------------------------------------

SLOP = [
    # Hedging filler that carries no information.
    r"it(?:'s| is) (?:important|worth|crucial|essential|vital) to (?:note|remember|understand|mention)",
    r"it should be noted that",
    r"needless to say",
    r"as we (?:have )?(?:already )?(?:mentioned|discussed|seen) (?:above|earlier|previously)",
    r"in (?:today|this day and age)'?s? (?:fast[- ]paced|modern|ever[- ]changing|digital) (?:world|landscape|era)",
    r"in the (?:ever[- ]evolving|rapidly changing) (?:world|landscape|field) of",
    # Consultant vapour.
    r"\bleverage\b(?!s\b)",
    r"\bdelve into\b",
    r"\bdive deep(?:er)? into\b",
    r"\bunlock the (?:power|potential)\b",
    r"\bharness the (?:power|potential)\b",
    r"\bgame[- ]chang(?:er|ing)\b",
    r"\bparadigm shift\b",
    r"\bseamless(?:ly)? integrat",
    r"\brobust and scalable\b",
    r"\bcutting[- ]edge\b",
    # The hyphenated adjective is marketing. "the state of the art" as a plain
    # noun phrase is a real thing you are allowed to refer to.
    r"\bstate-of-the-art\b",
    r"\bbest[- ]in[- ]class\b",
    r"\bsupercharge\b",
    r"\bsynerg",
    r"\bholistic approach\b",
    r"\btreasure trove\b",
    r"\bmyriad of\b",
    r"\bplethora of\b",
    r"\bnavigate the complexit",
    r"\btapestry\b",
    r"\brealm of\b",
    r"\blandscape of\b",
    # Closing-paragraph tics.
    r"in conclusion,",
    r"to sum(?:mari[sz]e|ming up),",
    r"at the end of the day,",
    r"the (?:bottom|key) (?:line|takeaway) is (?:that|simply)",
    r"only time will tell",
    r"the possibilities are endless",
    # LLM-assistant register.
    r"\bcertainly[,!]",
    r"\bgreat question\b",
    r"let'?s (?:dive|jump) (?:in|right in)\b",
    r"\bi hope this helps\b",
    r"\bfeel free to\b",
    r"\bremember,? (?:that )?the key\b",
    # Empty intensifiers stacked on empty nouns.
    r"\bincredibly powerful\b",
    r"\bextremely important\b",
    r"\bvery unique\b",
    r"\btruly remarkable\b",
]
SLOP_RE = [(p, re.compile(p, re.IGNORECASE)) for p in SLOP]

# "Not just X, but Y" and "isn't about X, it's about Y" are the two most
# recognisable generated-prose cadences. Allowed sparingly; flagged past a budget.
CADENCE = [
    ("not-just-but", re.compile(r"\bnot (?:just|only) [^.;:]{3,60}?,? but\b", re.I)),
    ("isnt-about-its-about", re.compile(r"\bis(?:n't| not) about [^.;:]{3,60}?[,.] it'?s about\b", re.I)),
    ("thats-not-x-thats-y", re.compile(r"\bthat'?s not [^.;:]{3,40}?\. that'?s\b", re.I)),
]
CADENCE_BUDGET = 3  # per chapter

# ---------------------------------------------------------------------------
# LaTeX stripping
# ---------------------------------------------------------------------------

# Code is not prose. These are the listing environments defined in
# book/preamble.tex, plus the standard ones.
VERBATIM_ENVS = (
    "verbatim", "Verbatim", "lstlisting", "minted", "tikzpicture",
    "wire", "http", "py", "ts", "sh", "plain",
)

_ENV_RE = re.compile(
    r"\\begin\{(" + "|".join(VERBATIM_ENVS) + r")\*?\}.*?\\end\{\1\*?\}",
    re.DOTALL,
)
_INLINE_VERB_RE = re.compile(r"\\(?:verb|lstinline)(.)(.*?)\1")
_COMMENT_RE = re.compile(r"(?<!\\)%.*$", re.MULTILINE)
# Inline math, but an escaped `\$` is a dollar sign in prose, not a delimiter.
# Getting this wrong pairs a price with a later `$` and silently swallows every
# paragraph in between, which is a great way to lint nothing at all.
_MATH_RE = re.compile(r"(?<!\\)\$(?:[^$\\]|\\.)*?(?<!\\)\$")
# Commands whose *argument* is code or a label, not prose.
_CODEARG_RE = re.compile(
    r"\\(?:code|texttt|path|url|href|label|ref|cite|includegraphics|input|include"
    r"|hypertarget|index|figref|chapref|secref|tabref)\s*(\[[^\]]*\])?\{[^{}]*\}"
)


def strip_latex(text: str) -> str:
    """Reduce a .tex file to the prose a reader actually reads.

    Replaces removed spans with equal-length blanks where cheap, so that
    reported line numbers stay accurate.
    """

    def blank(m: re.Match) -> str:
        return re.sub(r"[^\n]", " ", m.group(0))

    text = _ENV_RE.sub(blank, text)
    text = _COMMENT_RE.sub(blank, text)
    text = _INLINE_VERB_RE.sub(blank, text)
    text = _MATH_RE.sub(blank, text)
    for _ in range(3):  # nested braces, a few passes is plenty
        text = _CODEARG_RE.sub(blank, text)
    # Remaining commands: drop the backslash-name, keep braced prose.
    text = re.sub(r"\\[a-zA-Z@]+\*?", lambda m: " " * len(m.group(0)), text)
    text = text.replace("{", " ").replace("}", " ")
    return text


# ---------------------------------------------------------------------------
# Rule 3: repetition
# ---------------------------------------------------------------------------

_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+")
STOPWORDS = {
    "the", "a", "an", "and", "or", "but", "of", "to", "in", "on", "for", "is",
    "are", "was", "were", "be", "been", "it", "its", "that", "this", "with",
    "as", "at", "by", "from", "you", "your", "we", "our", "not", "if", "then",
    "than", "so", "do", "does", "can", "will", "there", "their", "they", "has",
    "have", "had", "what", "when", "which", "who", "how", "one", "all", "no",
}


def fingerprint(sentence: str) -> frozenset[str]:
    words = re.findall(r"[a-z]+", sentence.lower())
    return frozenset(w for w in words if w not in STOPWORDS and len(w) > 3)


def jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


# ---------------------------------------------------------------------------
# Findings
# ---------------------------------------------------------------------------


@dataclass
class Finding:
    path: Path
    line: int
    rule: str
    message: str
    level: str = "error"

    def render(self) -> str:
        rel = self.path.relative_to(ROOT) if self.path.is_absolute() else self.path
        tag = "error" if self.level == "error" else "warn "
        return f"{rel}:{self.line}: {tag} [{self.rule}] {self.message}"


def line_of(text: str, index: int) -> int:
    return text.count("\n", 0, index) + 1


def excerpt(text: str, index: int, span: int = 46) -> str:
    lo = max(0, index - span // 2)
    hi = min(len(text), index + span)
    return " ".join(text[lo:hi].split())


# ---------------------------------------------------------------------------
# Per-file checks
# ---------------------------------------------------------------------------


def lint_file(path: Path, sentence_index: dict) -> list[Finding]:
    raw = path.read_text(encoding="utf-8")
    prose = strip_latex(raw)
    findings: list[Finding] = []
    is_chapter = path.parent.name == "chapters"

    # Rule 1. Dashes are checked against the raw source, minus verbatim blocks,
    # because a `---` inside a code listing is legitimate.
    dash_scope = _ENV_RE.sub(lambda m: re.sub(r"[^\n]", " ", m.group(0)), raw)
    dash_scope = _INLINE_VERB_RE.sub(lambda m: re.sub(r"[^\n]", " ", m.group(0)), dash_scope)
    for name, pat in DASH_PATTERNS:
        for m in pat.finditer(dash_scope):
            findings.append(
                Finding(path, line_of(dash_scope, m.start()), "dash",
                        f"{name}: ...{excerpt(dash_scope, m.start())}...")
            )

    # Rule 2. Slop.
    for pattern, pat in SLOP_RE:
        for m in pat.finditer(prose):
            findings.append(
                Finding(path, line_of(prose, m.start()), "slop",
                        f'"{m.group(0).strip()}" ...{excerpt(prose, m.start())}...')
            )

    for name, pat in CADENCE:
        hits = list(pat.finditer(prose))
        if len(hits) > CADENCE_BUDGET:
            for m in hits[CADENCE_BUDGET:]:
                findings.append(
                    Finding(path, line_of(prose, m.start()), "cadence",
                            f'"{name}" used {len(hits)} times, budget is {CADENCE_BUDGET}: '
                            f"...{excerpt(prose, m.start())}...")
                )

    # Rule 4. No epigraphs. They were a house rule until the chapters were read
    # end to end: every one had grown a paragraph explaining how the quotation
    # bore on the topic, and that paragraph was always the weakest on the page.
    for m in re.finditer(r"\\epigraph", raw):
        findings.append(Finding(path, line_of(raw, m.start()), "epigraph",
                                "chapters open on their argument, not a quotation"))

    # Rule 5. Listings are typeset by `listings` under T1, which cannot render
    # characters outside Latin-1. A stray arrow or CJK glyph in a code block is
    # a hard pdflatex error, and the message it produces names no file or line.
    for m in _ENV_RE.finditer(raw):
        body = m.group(0)
        offenders = sorted({c for c in body if ord(c) > 0xFF})
        if offenders:
            findings.append(
                Finding(path, line_of(raw, m.start()), "listing-encoding",
                        f"{m.group(1)} listing contains characters T1 cannot set: "
                        + " ".join(f"{c!r} (U+{ord(c):04X})" for c in offenders))
            )

    # Rule 5b. The same constraint applies to prose, and it bit once: a chapter
    # about homograph attacks reached for a Cyrillic glyph to demonstrate one.
    # pdflatex refuses it and names no file, so the whole build dies pointing at
    # a page number. Name the codepoint instead of typesetting it.
    #
    # A handful of characters ARE set up: the preamble declares them, and en
    # dashes and accented Latin come through the T1 encoding fine.
    allowed = set(" –‘’“”…×→")
    stripped = _ENV_RE.sub(" ", raw)
    offenders = sorted({c for c in stripped if ord(c) > 0xFF and c not in allowed})
    if offenders:
        first = stripped.index(offenders[0])
        findings.append(
            Finding(path, line_of(raw, first), "prose-encoding",
                    "prose contains characters T1 cannot set: "
                    + " ".join(f"{c!r} (U+{ord(c):04X})" for c in offenders))
        )

    # Rule 3. Repetition. Collect first, compare globally after all files load.
    #
    # The closing summary of a chapter restates that chapter on purpose, so it
    # is exempt. Policing it would just push the summary into worse phrasings
    # of the same points.
    summary = re.search(r"\\section\*?\{What to remember\}", raw)
    limit = len(prose) if summary is None else summary.start()

    # Appendices are condensed restatements of the chapters by design: a
    # checklist that avoided repeating the chapter would not be a checklist.
    # They still get every other rule.
    if path.parent.name == "appendices":
        return findings

    for m in re.finditer(r"[^.!?\n][^.!?]{40,}[.!?]", prose[:limit]):
        sentence = " ".join(m.group(0).split())
        fp = fingerprint(sentence)
        if len(fp) < 6:
            continue
        sentence_index[path].append((line_of(prose, m.start()), sentence, fp))

    return findings


def lint_repetition(sentence_index: dict, threshold: float = 0.72) -> list[Finding]:
    """Compare every long sentence against every other, cheaply.

    An inverted index on rare-ish words keeps this near-linear instead of
    quadratic over the whole book.
    """
    postings: dict[str, list[tuple[Path, int, str, frozenset[str]]]] = defaultdict(list)
    findings: list[Finding] = []
    seen: set[tuple] = set()

    for path, entries in sentence_index.items():
        for line, sentence, fp in entries:
            candidates: set[tuple] = set()
            for word in fp:
                for cand in postings.get(word, ()):
                    candidates.add(cand)
            for cpath, cline, csentence, cfp in candidates:
                score = jaccard(fp, cfp)
                if score < threshold:
                    continue
                key = tuple(sorted([(str(path), line), (str(cpath), cline)]))
                if key in seen:
                    continue
                seen.add(key)
                where = (
                    f"line {cline}" if cpath == path
                    else f"{cpath.relative_to(ROOT)}:{cline}"
                )
                findings.append(
                    Finding(path, line, "repetition",
                            f"{score:.0%} overlap with {where}: "
                            f'"{sentence[:70]}..." / "{csentence[:70]}..."',
                            level="warn")
                )
            for word in fp:
                postings[word].append((path, line, sentence, fp))
    return findings


# ---------------------------------------------------------------------------


def collect(paths: list[str]) -> list[Path]:
    if paths:
        return [Path(p).resolve() for p in paths]
    out: list[Path] = []
    for sub in ("chapters", "appendices", "frontmatter"):
        out.extend(sorted((ROOT / "book" / sub).glob("*.tex")))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("paths", nargs="*")
    ap.add_argument("--quiet", action="store_true", help="exit code only")
    ap.add_argument("--warnings", action="store_true", help="also show warn-level findings")
    args = ap.parse_args()

    files = collect(args.paths)
    if not files:
        print("no .tex files found", file=sys.stderr)
        return 0

    sentence_index: dict[Path, list] = defaultdict(list)
    findings: list[Finding] = []
    for path in files:
        findings.extend(lint_file(path, sentence_index))
    findings.extend(lint_repetition(sentence_index))

    errors = [f for f in findings if f.level == "error"]
    warns = [f for f in findings if f.level == "warn"]

    if not args.quiet:
        shown = errors + (warns if args.warnings else [])
        for f in sorted(shown, key=lambda f: (str(f.path), f.line)):
            print(f.render())
        words = sum(
            len(re.findall(r"[A-Za-z][A-Za-z'-]+", strip_latex(p.read_text(encoding="utf-8"))))
            for p in files
        )
        print(
            f"\n{len(files)} files, ~{words:,} words of prose, "
            f"{len(errors)} error(s), {len(warns)} warning(s)"
            + ("" if args.warnings else " (use --warnings to list warnings)")
        )

    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
