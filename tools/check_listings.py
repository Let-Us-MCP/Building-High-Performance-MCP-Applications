#!/usr/bin/env python3
"""Verify that Python listings in the book match the real Meridian source.

The book claims every listing is extracted from code that runs. This checks it.

Listings are edited for the page: comments are trimmed, bodies are elided with
`...`, and a method is shown without its class. So an exact match is the wrong
test. Instead, for each listing this extracts the identifying lines (the `def`
line, distinctive string literals, distinctive expressions) and confirms they
appear in a real source file, in order.

Findings are one of:

  MISSING   a signature the book shows that no source file contains
  DRIFT     the signature exists but a quoted line inside it does not
  ORPHAN    a listing that matches nothing and names nothing checkable

    python3 tools/check_listings.py
    python3 tools/check_listings.py --verbose
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BOOK = ROOT / "book"
SRC = ROOT / "meridian"

PY_ENV = re.compile(r"\\begin\{py\}\n(.*?)\\end\{py\}", re.DOTALL)

# Lines that carry no identifying information.
NOISE = re.compile(
    r"^\s*(?:\.\.\.|#|\"\"\"|'''|\)|\}|\]|else:|try:|except.*:|pass|return\s*$)"
    r"|^\s*$")


def join_continuations(text: str) -> str:
    """Join lines that continue an open bracket, so a signature is one line.

    Both the book and the source wrap long signatures, and they wrap them at
    different columns. Comparing wrapped text to wrapped text produces noise, so
    both sides get flattened the same way first.
    """
    out, buf, depth = [], "", 0
    for raw in text.splitlines():
        line = raw.strip()
        buf = (buf + " " + line).strip() if buf else line
        depth += line.count("(") + line.count("[") + line.count("{")
        depth -= line.count(")") + line.count("]") + line.count("}")
        if depth <= 0:
            out.append(" ".join(buf.split()))
            buf, depth = "", 0
    if buf:
        out.append(" ".join(buf.split()))
    return "\n".join(out)


def source_files() -> dict[Path, str]:
    return {p: p.read_text(encoding="utf-8")
            for p in sorted(SRC.rglob("*.py"))}


def book_files() -> list[Path]:
    out = []
    for sub in ("chapters", "appendices", "frontmatter"):
        out.extend(sorted((BOOK / sub).glob("*.tex")))
    return out


def _signature_present(sig: str, blob: str) -> bool:
    """True if a `def`/`class` line with this name and parameters exists.

    Book listings drop type hints and defaults to fit the page, so comparing
    the whole signature text produces false alarms. The name plus the parameter
    names is enough to identify it.
    """
    if sig in blob:
        return True
    m = re.match(r"^(?:async )?(def|class) (\w+)\s*\((.*?)\)?\s*(?:->.*)?$", sig)
    if not m:
        return sig in blob
    kind, name, params = m.group(1), m.group(2), m.group(3) or ""
    if f"{kind} {name}(" not in blob:
        return False
    wanted = {p.strip().split(":")[0].split("=")[0].strip()
              for p in params.split(",")}
    wanted = {w for w in wanted if w and w not in ("self", "*", "**", "cls")}
    for m2 in re.finditer(rf"{kind} {re.escape(name)}\((.*)", blob):
        have = m2.group(1)
        if all(w in have for w in wanted):
            return True
    return False


def normalise(line: str) -> str:
    """Collapse whitespace so wrapping differences do not matter."""
    return " ".join(line.split())


def identifying_lines(body: str) -> tuple[list[str], list[str]]:
    """Return (signatures, other identifying lines) from a listing."""
    sigs, others = [], []
    for raw in body.splitlines():
        line = normalise(raw)
        if not line or NOISE.match(raw):
            continue
        if re.match(r"^(?:async )?def \w+", line) or line.startswith("class "):
            sigs.append(line.rstrip(":").rstrip())
        elif len(line) > 24:
            others.append(line)
    return sigs, others


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    sources = source_files()
    blob = "\n".join(join_continuations(text) for text in sources.values())
    # A second, whitespace-free view: the book rewraps code to fit the page, and
    # a line break moving does not make a listing wrong.
    dense = re.sub(r"\s+", "", blob)

    findings: list[str] = []
    checked = matched = 0

    for path in book_files():
        text = path.read_text(encoding="utf-8")
        for m in PY_ENV.finditer(text):
            body = m.group(1)
            # Skip listings that are not Python: JSON, config, Dockerfiles.
            if body.lstrip().startswith(("{", "[", "//", "FROM ")):
                continue
            # A listing may declare itself a sketch. The book marks those, and
            # the marker is the promise that it is not claiming to be extracted.
            if "# sketch:" in body:
                continue
            line_no = text.count("\n", 0, m.start()) + 1
            sigs, others = identifying_lines(join_continuations(body))
            if not sigs and not others:
                continue
            checked += 1

            # Match a signature by its name and parameter names, so a book
            # listing that drops a type hint or a default still matches.
            missing_sigs = [s for s in sigs if not _signature_present(s, blob)]
            if missing_sigs:
                findings.append(
                    f"{path.name}:{line_no}: MISSING signature(s): "
                    + "; ".join(s[:70] for s in missing_sigs[:2]))
                continue

            if sigs:
                # Signature found. Check a sample of the body lines too.
                sample = [o for o in others if not o.startswith(("@", "\"", "'"))][:6]
                drifted = [o for o in sample
                           if o not in blob
                           and re.sub(r"\s+", "", o) not in dense]
                # A listing is edited for the page, so some lines will differ.
                # Only flag it when most of the sample is absent.
                if sample and len(drifted) > len(sample) // 2:
                    findings.append(
                        f"{path.name}:{line_no}: DRIFT in {sigs[0][:50]}: "
                        + "; ".join(d[:60] for d in drifted[:2]))
                else:
                    matched += 1
                continue

            # No signature: require at least one distinctive line to exist.
            hits = [o for o in others
                    if o in blob or re.sub(r"\s+", "", o) in dense]
            if hits:
                matched += 1
            elif any(k in body for k in ("meridian", "server.", "client.",
                                         "ctx.", "self.")):
                findings.append(
                    f"{path.name}:{line_no}: ORPHAN listing, nothing matched: "
                    + normalise(body)[:70])

    for f in findings:
        print(f)

    print(f"\n{checked} Python listings checked against {len(sources)} source "
          f"files: {matched} matched, {len(findings)} problem(s)")
    if args.verbose and not findings:
        print("Every listing traces to real code.")
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
