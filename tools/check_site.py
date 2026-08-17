#!/usr/bin/env python3
"""Verify the built site: every internal link resolves, no unresolved references.

Runs against `docs/` after `make site`. Catches the two failure modes that are
invisible until a reader hits them: a link to a page that was renamed, and a
cross-reference that quietly typeset as `??`.

    python3 tools/check_site.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"

LINK_RE = re.compile(r'(?:href|src)="([^"#:]*)(?:#([^"]*))?"')
# `??` is only a problem in prose. Inside <code> it is ordinary text, and this
# book happens to discuss the `??` failure mode in Chapter 15.
CODE_RE = re.compile(r"<code>.*?</code>|<pre>.*?</pre>", re.DOTALL)


def main() -> int:
    if not DOCS.exists():
        print("docs/ not found; run `make site` first", file=sys.stderr)
        return 1

    pages = {p.name for p in DOCS.glob("*.html")}
    assets = {str(p.relative_to(DOCS)) for p in DOCS.rglob("*") if p.is_file()}
    anchors: dict[str, set[str]] = {}
    for page in DOCS.glob("*.html"):
        text = page.read_text(encoding="utf-8")
        anchors[page.name] = set(re.findall(r'id="([^"]+)"', text))

    problems: list[str] = []

    for page in sorted(DOCS.glob("*.html")):
        text = page.read_text(encoding="utf-8")

        for m in LINK_RE.finditer(text):
            target, frag = m.group(1), m.group(2)
            if target.startswith(("http", "mailto", "//")):
                continue
            if target and target not in pages and target not in assets:
                problems.append(f"{page.name}: link to missing {target!r}")
                continue
            if frag:
                where = target or page.name
                if where in anchors and frag not in anchors[where]:
                    problems.append(
                        f"{page.name}: link to {where}#{frag}, no such anchor")

        prose = CODE_RE.sub("", text)
        if "??" in prose:
            problems.append(f"{page.name}: unresolved cross-reference (??)")

    for p in problems[:30]:
        print("BROKEN:", p)
    extra = len(problems) - 30
    if extra > 0:
        print(f"... and {extra} more")

    print(f"\n{len(pages)} pages, {len(assets)} assets, "
          f"{len(problems)} problem(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
