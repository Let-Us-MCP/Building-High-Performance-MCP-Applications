#!/usr/bin/env python3
"""Build the website from the same LaTeX the PDF is built from.

There is exactly one copy of every sentence in this book. The PDF and the site
are two renderings of it, which is the only arrangement that does not drift.

How it works:

  1. Read `book/build/book.aux` for the label numbering LaTeX already computed,
     so "Chapter 5" on the web is the same Chapter 5 as in the PDF.
  2. Rewrite the handful of custom macros from `book/preamble.tex` into
     constructs pandoc understands, leaving unique text markers behind.
  3. Run pandoc per file.
  4. Turn the markers into semantic HTML (asides, figures, cross-links).
  5. Wrap in a hand-written responsive template.

Requires pandoc. Figures must already exist as SVG (`make figures`).

    python3 tools/build_site.py
"""

from __future__ import annotations

import html
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BOOK = ROOT / "book"
AUX = BOOK / "build" / "book.aux"
OUT = ROOT / "docs"

SITE_TITLE = "Building High-Performance MCP Applications"
SITE_SUBTITLE = ("What every AI application developer should know "
                 "about the Model Context Protocol")
REPO = "https://github.com/Let-Us-MCP/Building-High-Performance-MCP-Applications"

# ---------------------------------------------------------------------------
# Structure
# ---------------------------------------------------------------------------


@dataclass
class Page:
    slug: str
    source: Path
    title: str
    part: str | None = None
    kind: str = "chapter"


def structure() -> list[Page]:
    ch = BOOK / "chapters"
    ap = BOOK / "appendices"
    fm = BOOK / "frontmatter"
    return [
        Page("preface", fm / "preface.tex", "Preface", None, "front"),
        Page("how-to-read", fm / "howtoread.tex", "How to read this book", None, "front"),

        Page("ch01", ch / "ch01.tex", "A Primer on Latency, Tokens, and Context",
             "Part I. Protocol Fundamentals"),
        Page("ch02", ch / "ch02.tex", "The MCP Architectural Model"),
        Page("ch03", ch / "ch03.tex", "Building Blocks of the Transport"),
        Page("ch04", ch / "ch04.tex", "Authorization on the Wire"),

        Page("ch05", ch / "ch05.tex", "Tools: The Execution Layer",
             "Part II. The Primitives"),
        Page("ch06", ch / "ch06.tex", "Resources: The Context Layer"),
        Page("ch07", ch / "ch07.tex", "Prompts: Workflow Blueprints"),
        Page("ch08", ch / "ch08.tex", "Multi Round-Trip Requests"),
        Page("ch09", ch / "ch09.tex", "Extensions and Tasks"),
        Page("ch10", ch / "ch10.tex", "MCP Apps: Interactive Interfaces"),

        Page("ch11", ch / "ch11.tex", "Building Servers",
             "Part III. Building MCP Applications"),
        Page("ch12", ch / "ch12.tex", "Building Hosts and Clients"),
        Page("ch13", ch / "ch13.tex", "The Agent Loop, State, and Memory"),
        Page("ch14", ch / "ch14.tex", "Multi-Agent Topologies"),

        Page("ch15", ch / "ch15.tex", "Testing and Debugging",
             "Part IV. Performance Engineering"),
        Page("ch16", ch / "ch16.tex", "Measuring MCP Applications"),
        Page("ch17", ch / "ch17.tex", "Optimizing for Latency, Tokens, and Cost"),
        Page("ch18", ch / "ch18.tex", "Deployment and Operations"),

        Page("ch19", ch / "ch19.tex", "Threat Modeling Agentic Applications",
             "Part V. Security and Trust"),
        Page("ch20", ch / "ch20.tex", "Access Control, Audit, and MCP in the Enterprise"),

        Page("epilogue", ch / "epilogue.tex", "Epilogue: The Evolving Protocol",
             "Back matter", "front"),
        Page("appendix-a", ap / "appA.tex", "A. Method and Message Reference",
             None, "appendix"),
        Page("appendix-b", ap / "appB.tex", "B. Transport Decision Matrix",
             None, "appendix"),
        Page("appendix-c", ap / "appC.tex", "C. Performance Checklist",
             None, "appendix"),
        Page("appendix-d", ap / "appD.tex", "D. Security Hardening Checklist",
             None, "appendix"),
        Page("appendix-e", ap / "appE.tex", "E. The Meridian Companion Repository",
             None, "appendix"),
        Page("appendix-f", ap / "appF.tex", "F. Sources and Further Reading",
             None, "appendix"),
        Page("about", fm / "author.tex", "About the author", None, "front"),
    ]


# ---------------------------------------------------------------------------
# Label numbering, taken from LaTeX so the site cannot disagree with the PDF
# ---------------------------------------------------------------------------

_NEWLABEL = re.compile(r"\\newlabel\{([^}]+)\}\{\{([^}]*)\}")


def read_labels() -> dict[str, str]:
    if not AUX.exists():
        print("warning: book/build/book.aux not found; run `make book` first. "
              "Cross-references will be unnumbered.", file=sys.stderr)
        return {}
    out: dict[str, str] = {}
    for m in _NEWLABEL.finditer(AUX.read_text(errors="replace")):
        out[m.group(1)] = m.group(2)
    return out


def label_home(pages: list[Page]) -> dict[str, str]:
    """Which page each label lives on, so a reference can link to it."""
    home: dict[str, str] = {}
    for page in pages:
        text = page.source.read_text(encoding="utf-8")
        for m in re.finditer(r"\\label\{([^}]+)\}", text):
            home[m.group(1)] = page.slug
        for m in re.finditer(
                r"\\bookfig(?:\[[^\]]*\])?\{[^{}]*\}\{(?:[^{}]|\{[^{}]*\})*\}\{([^{}]+)\}",
                text):
            home["fig:" + m.group(1)] = page.slug
    return home


# ---------------------------------------------------------------------------
# LaTeX preprocessing
# ---------------------------------------------------------------------------

CODE_ENVS = {
    "wire": "json", "http": "http", "py": "python",
    "ts": "javascript", "sh": "bash", "plain": "text",
}
BOX_ENVS = {
    "legacybox": ("legacy", "Legacy"),
    "measurebox": ("measured", "Measured"),
    "dangerbox": ("threat", "Threat"),
    "notebox": ("note", None),
}

M_BOX = "\u2063BOX\u2063"
M_ENDBOX = "\u2063ENDBOX\u2063"
M_KEY = "\u2063KEY\u2063"
M_EPI = "\u2063EPI\u2063"


def _balanced(text: str, start: int) -> tuple[str, int]:
    """Read a brace group starting at `text[start] == '{'`. Returns (body, end)."""
    assert text[start] == "{"
    depth, i = 0, start
    while i < len(text):
        if text[i] == "{" and (i == 0 or text[i - 1] != "\\"):
            depth += 1
        elif text[i] == "}" and text[i - 1] != "\\":
            depth -= 1
            if depth == 0:
                return text[start + 1:i], i + 1
        i += 1
    raise ValueError("unbalanced braces")


def preprocess(text: str, labels: dict[str, str], home: dict[str, str],
               slug: str) -> str:
    # Drop things that are page-layout only.
    text = re.sub(r"\\(?:cleardoublepage|clearpage|newpage|vfill|centering|"
                  r"footnotesize|small|toprule|midrule|bottomrule|par)\b", "", text)
    text = re.sub(r"\\vspace\*?\{[^}]*\}", "", text)
    text = re.sub(r"\\markboth\{[^}]*\}\{[^}]*\}", "", text)
    text = re.sub(r"\\addcontentsline\{[^}]*\}\{[^}]*\}\{[^}]*\}", "", text)
    text = re.sub(r"\\thispagestyle\{[^}]*\}", "", text)

    # `enumitem` options are layout-only, and pandoc's list reader drops the
    # \item[term] labels of a description list when they are present.
    text = re.sub(r"(\\begin\{(?:itemize|enumerate|description)\})\[[^\]]*\]",
                  r"\1", text)

    # Code environments -> lstlisting, whose `language=` pandoc understands.
    for env, lang in CODE_ENVS.items():
        text = re.sub(
            rf"\\begin\{{{env}\}}(?:\[[^\]]*\])?\n(.*?)\\end\{{{env}\}}",
            lambda m, l=lang: ("\\begin{lstlisting}[language=" + l + "]\n"
                               + m.group(1) + "\\end{lstlisting}"),
            text, flags=re.DOTALL)

    # Boxes -> a quote carrying a unique marker we recover after pandoc.
    for env, (cls, default_title) in BOX_ENVS.items():
        pattern = re.compile(rf"\\begin\{{{env}\}}\{{")
        while True:
            m = pattern.search(text)
            if not m:
                break
            title, after = _balanced(text, m.end() - 1)
            end = text.index(f"\\end{{{env}}}", after)
            body = text[after:end]
            label = f"{default_title}: {title}" if default_title else title
            text = (text[:m.start()]
                    + "\n\\begin{quote}\n\\textbf{" + M_BOX + cls + M_BOX
                    + label + M_BOX + "}\n\n" + body
                    + "\n\n" + M_ENDBOX + "\n\n\\end{quote}\n"
                    + text[end + len(f"\\end{{{env}}}"):])

    # \keyidea{...}
    while True:
        m = re.search(r"\\keyidea\{", text)
        if not m:
            break
        body, end = _balanced(text, m.end() - 1)
        text = (text[:m.start()] + "\n\\begin{quote}\n\\textbf{" + M_KEY
                + "}\n\n" + body + "\n\n" + M_ENDBOX + "\n\n\\end{quote}\n" + text[end:])

    # \epigraph{quote}{attribution}
    while True:
        m = re.search(r"\\epigraph\{", text)
        if not m:
            break
        quote, i = _balanced(text, m.end() - 1)
        attrib, end = _balanced(text, i)
        text = (text[:m.start()] + "\n\\begin{quote}\n\\textbf{" + M_EPI
                + "}\n\n\\emph{" + quote + "}\n\n" + attrib
                + "\n\n" + M_ENDBOX + "\n\n\\end{quote}\n" + text[end:])

    # \bookfig[width]{file}{caption}{label} -> a normal figure pandoc renders.
    def bookfig(m: re.Match) -> str:
        rest = m.group(0)
        i = rest.index("{")
        name, i2 = _balanced(rest, i)
        caption, i3 = _balanced(rest, i2)
        label, _ = _balanced(rest, i3)
        return (f"\n\\begin{{figure}}\n"
                f"\\includegraphics{{figures/{name}.svg}}\n"
                f"\\caption{{{caption}}}\n\\label{{fig:{label}}}\n"
                f"\\end{{figure}}\n")

    text = re.sub(
        r"\\bookfig(?:\[[^\]]*\])?\{[^{}]*\}\{(?:[^{}]|\{[^{}]*\})*\}\{[^{}]+\}",
        bookfig, text)

    # Cross-references, resolved from the .aux so they match the PDF.
    def ref(prefix: str, fmt: str):
        def sub(m: re.Match) -> str:
            key = prefix + m.group(1)
            num = labels.get(key)
            target = home.get(key)
            body = fmt.format(num) if num else fmt.format("?")
            if target is None:
                return body
            href = f"{target}.html#{key}" if target != slug else f"#{key}"
            return f"\\href{{{href}}}{{{body}}}"
        return sub

    text = re.sub(r"\\chapref\{([^}]+)\}", ref("ch:", "Chapter {}"), text)
    text = re.sub(r"\\figref\{([^}]+)\}", ref("fig:", "Figure {}"), text)
    text = re.sub(r"\\secref\{([^}]+)\}", ref("sec:", "\u00a7{}"), text)
    text = re.sub(r"\\tabref\{([^}]+)\}", ref("tab:", "Table {}"), text)

    def plain_ref(m: re.Match) -> str:
        key = m.group(1)
        num = labels.get(key, "?")
        target = home.get(key)
        if target is None:
            return num
        href = f"{target}.html#{key}" if target != slug else f"#{key}"
        return f"\\href{{{href}}}{{{num}}}"

    text = re.sub(r"\\ref\{([^}]+)\}", plain_ref, text)

    # `\thead{X}` is a table-header style; keep the text.
    text = re.sub(r"\\thead\{", "\\\\textbf{", text)
    # tabularx column specs pandoc does not know.
    text = text.replace("{\\textwidth}", "")
    text = re.sub(r"\\begin\{tabularx\}", "\\\\begin{tabular}", text)
    text = re.sub(r"\\end\{tabularx\}", "\\\\end{tabular}", text)

    return text


# ---------------------------------------------------------------------------
# Pandoc
# ---------------------------------------------------------------------------


def to_html(tex: str) -> str:
    proc = subprocess.run(
        ["pandoc", "-f", "latex", "-t", "html5", "--no-highlight",
         "--wrap=none", "--section-divs"],
        input=tex, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr[-2000:])
    return proc.stdout


# ---------------------------------------------------------------------------
# HTML post-processing
# ---------------------------------------------------------------------------


def postprocess(body: str) -> str:
    # Boxes: <blockquote><p><strong>MARKER cls MARKER title MARKER</strong></p>
    def box(m: re.Match) -> str:
        cls, title = m.group(1), m.group(2)
        return f'<aside class="box {cls}"><p class="box-title">{title}</p>'

    body = re.sub(
        rf"<blockquote>\s*<p><strong>{M_BOX}([a-z]+){M_BOX}(.*?){M_BOX}</strong></p>",
        box, body, flags=re.DOTALL)
    body = re.sub(rf"<blockquote>\s*<p><strong>{M_KEY}</strong></p>",
                  '<aside class="keyidea">', body)
    body = re.sub(rf"<blockquote>\s*<p><strong>{M_EPI}</strong></p>",
                  '<aside class="epigraph">', body)
    # Close them: the marker paragraph, then the blockquote tag.
    body = re.sub(rf"<p>{M_ENDBOX}</p>\s*</blockquote>", "</aside>", body)
    body = re.sub(rf"{M_ENDBOX}\s*</blockquote>", "</aside>", body)
    # Any stragglers.
    body = body.replace(M_ENDBOX, "").replace(M_BOX, "").replace(M_KEY, "")
    body = body.replace(M_EPI, "")

    return body


# ---------------------------------------------------------------------------
# Template
# ---------------------------------------------------------------------------

CSS = """
:root{
  --ink:#15181D; --muted:#5B6472; --rule:#D8DDE4; --wash:#F5F6F8;
  --paper:#FFFFFF; --accent:#0F5C8C; --accent-soft:#E7F0F6;
  --warm:#B4531A; --warm-soft:#FBF0E7; --good:#2C6E49; --good-soft:#E9F2EC;
  --danger:#9B2226; --danger-soft:#FAECEC;
  --sans:-apple-system,BlinkMacSystemFont,"Segoe UI",Inter,Roboto,sans-serif;
  --serif:"Iowan Old Style","Palatino Linotype",Palatino,Georgia,serif;
  --mono:ui-monospace,SFMono-Regular,"SF Mono",Menlo,Consolas,monospace;
}
@media (prefers-color-scheme: dark){
  :root{
    --ink:#E7EAEE; --muted:#98A2B0; --rule:#2C333D; --wash:#171B21;
    --paper:#0F1216; --accent:#63AEDC; --accent-soft:#14232E;
    --warm:#E08B4F; --warm-soft:#2A1D13; --good:#6FBF8E; --good-soft:#13221A;
    --danger:#E4796F; --danger-soft:#291516;
  }
}
*{box-sizing:border-box}
html{scroll-behavior:smooth}
body{margin:0;background:var(--paper);color:var(--ink);
  font-family:var(--serif);font-size:18px;line-height:1.62;
  -webkit-text-size-adjust:100%}
.layout{display:grid;grid-template-columns:288px minmax(0,1fr);
  max-width:1280px;margin:0 auto;gap:0}
nav.toc{position:sticky;top:0;height:100vh;overflow-y:auto;padding:26px 20px 60px;
  border-right:1px solid var(--rule);font-family:var(--sans);font-size:14px}
nav.toc .brand{display:block;font-weight:700;font-size:16px;color:var(--ink);
  text-decoration:none;line-height:1.3;margin-bottom:4px}
nav.toc .tagline{color:var(--muted);font-size:12.5px;margin:0 0 18px;line-height:1.45}
nav.toc .part{margin:18px 0 6px;font-size:11px;letter-spacing:.09em;
  text-transform:uppercase;color:var(--muted);font-weight:700}
nav.toc a{display:block;padding:4px 8px;margin-left:-8px;border-radius:5px;
  color:var(--muted);text-decoration:none}
nav.toc a:hover{background:var(--wash);color:var(--ink)}
nav.toc a.current{background:var(--accent-soft);color:var(--accent);font-weight:600}
main{padding:44px 52px 120px;min-width:0;max-width:78ch}
h1,h2,h3,h4{font-family:var(--sans);line-height:1.25;color:var(--ink)}
h1{font-size:2.05em;margin:0 0 .55em;letter-spacing:-.015em}
h2{font-size:1.42em;margin:2em 0 .5em;padding-top:.35em}
h3{font-size:1.14em;margin:1.7em 0 .4em}
h4{font-size:1em;margin:1.4em 0 .3em;color:var(--muted)}
p{margin:0 0 1.05em}
a{color:var(--accent)}
code{font-family:var(--mono);font-size:.855em;background:var(--wash);
  padding:.12em .36em;border-radius:4px;word-break:break-word}
pre{background:var(--wash);border-left:3px solid var(--rule);
  border-radius:0 6px 6px 0;padding:14px 16px;overflow-x:auto;
  font-size:13.5px;line-height:1.5;margin:1.3em 0}
pre code{background:none;padding:0;font-size:inherit;word-break:normal}
pre.json,pre.http{background:var(--accent-soft);border-left-color:var(--accent)}
aside.box{border-left:3px solid var(--rule);background:var(--wash);
  padding:14px 18px;margin:1.5em 0;border-radius:0 6px 6px 0;font-size:.955em}
aside.box .box-title{font-family:var(--sans);font-weight:700;font-size:12px;
  letter-spacing:.07em;text-transform:uppercase;margin:0 0 .55em}
aside.box.legacy{border-left-color:var(--warm);background:var(--warm-soft)}
aside.box.legacy .box-title{color:var(--warm)}
aside.box.measured{border-left-color:var(--good);background:var(--good-soft)}
aside.box.measured .box-title{color:var(--good)}
aside.box.threat{border-left-color:var(--danger);background:var(--danger-soft)}
aside.box.threat .box-title{color:var(--danger)}
aside.box.note .box-title{color:var(--muted)}
aside.keyidea{border-left:3px solid var(--accent);background:var(--accent-soft);
  padding:14px 18px;margin:1.6em 0;border-radius:0 6px 6px 0;
  font-family:var(--sans);font-style:italic;font-size:.97em}
aside.keyidea p:last-child,aside.box p:last-child{margin-bottom:0}
aside.keyidea pre,aside.keyidea code,aside.keyidea table{font-style:normal}
aside.epigraph{border:0;margin:0 0 2em;padding:0 0 0 clamp(0px,7%,52px);
  color:var(--muted);font-size:.97em}
aside.epigraph p{margin:0 0 .3em}
main figure{margin:2em 0;text-align:center}
main figure img{max-width:100%;height:auto;border-radius:6px}
@media (prefers-color-scheme: dark){
  main figure img{background:#fff;padding:10px;border-radius:8px}
}
figcaption{font-family:var(--sans);font-size:13.5px;color:var(--muted);
  margin-top:.7em;text-align:left;line-height:1.5}
.table-wrap{overflow-x:auto;margin:1.5em 0}
table{border-collapse:collapse;width:100%;font-family:var(--sans);font-size:14.5px}
th,td{text-align:left;padding:8px 12px;border-bottom:1px solid var(--rule);
  vertical-align:top}
thead th{border-bottom:2px solid var(--rule);font-weight:700}
caption{caption-side:bottom;font-size:13.5px;color:var(--muted);
  text-align:left;padding-top:.7em;line-height:1.5}
ul,ol{padding-left:1.35em;margin:0 0 1.05em}
li{margin-bottom:.32em}
dl{margin:0 0 1.05em}
dt{font-weight:700;font-family:var(--sans);font-size:.95em;margin-top:.75em}
dd{margin:.15em 0 0 1.35em}
hr{border:0;border-top:1px solid var(--rule);margin:2.5em 0}
.pager{display:flex;justify-content:space-between;gap:16px;margin-top:4em;
  padding-top:1.5em;border-top:1px solid var(--rule);font-family:var(--sans);
  font-size:14px}
.pager a{text-decoration:none;max-width:46%}
.pager .dir{display:block;font-size:11px;letter-spacing:.08em;
  text-transform:uppercase;color:var(--muted)}
.masthead{border-bottom:1px solid var(--rule);padding-bottom:1.8em;margin-bottom:2.2em}
.masthead .kicker{font-family:var(--sans);font-size:12px;letter-spacing:.1em;
  text-transform:uppercase;color:var(--accent);font-weight:700}
.lede{font-size:1.12em;color:var(--muted)}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));
  gap:14px;margin:1.8em 0}
.card{border:1px solid var(--rule);border-radius:9px;padding:15px 17px;
  font-family:var(--sans);font-size:14px}
.card h3{margin:0 0 .35em;font-size:15px}
.card p{margin:0;color:var(--muted);font-size:13.5px;line-height:1.5}
.menu-btn{display:none}
@media (max-width:940px){
  body{font-size:17px}
  .layout{grid-template-columns:1fr}
  nav.toc{position:static;height:auto;border-right:0;
    border-bottom:1px solid var(--rule);max-height:none}
  nav.toc.collapsed .toc-body{display:none}
  main{padding:28px 20px 90px}
  .menu-btn{display:inline-block;font-family:var(--sans);font-size:13px;
    background:var(--wash);border:1px solid var(--rule);color:var(--ink);
    border-radius:6px;padding:6px 11px;cursor:pointer;margin-bottom:10px}
}
"""

JS = """
(function(){
  var nav = document.querySelector('nav.toc');
  var btn = document.querySelector('.menu-btn');
  if(btn && nav){
    if(window.innerWidth <= 940) nav.classList.add('collapsed');
    btn.addEventListener('click', function(){ nav.classList.toggle('collapsed'); });
  }
  // Wrap tables so wide ones scroll rather than overflowing the page.
  document.querySelectorAll('main table').forEach(function(t){
    if(t.parentElement.classList.contains('table-wrap')) return;
    var w = document.createElement('div');
    w.className = 'table-wrap';
    t.parentNode.insertBefore(w, t);
    w.appendChild(t);
  });
})();
"""


def nav_html(pages: list[Page], current: str) -> str:
    parts = ['<button class="menu-btn" type="button">Contents</button>',
             '<div class="toc-body">',
             f'<a class="brand" href="index.html">{SITE_TITLE}</a>',
             f'<p class="tagline">{SITE_SUBTITLE}</p>']
    seen_appendix = False
    for page in pages:
        if page.part:
            parts.append(f'<div class="part">{html.escape(page.part)}</div>')
        if page.kind == "appendix" and not seen_appendix:
            parts.append('<div class="part">Appendices</div>')
            seen_appendix = True
        cls = ' class="current"' if page.slug == current else ""
        parts.append(f'<a href="{page.slug}.html"{cls}>{html.escape(page.title)}</a>')
    parts.append("</div>")
    return "\n".join(parts)


def pager(pages: list[Page], idx: int) -> str:
    bits = ['<div class="pager">']
    if idx > 0:
        p = pages[idx - 1]
        bits.append(f'<a href="{p.slug}.html"><span class="dir">Previous</span>'
                    f'{html.escape(p.title)}</a>')
    else:
        bits.append("<span></span>")
    if idx < len(pages) - 1:
        n = pages[idx + 1]
        bits.append(f'<a href="{n.slug}.html" style="text-align:right">'
                    f'<span class="dir">Next</span>{html.escape(n.title)}</a>')
    else:
        bits.append("<span></span>")
    bits.append("</div>")
    return "\n".join(bits)


def shell(title: str, nav: str, content: str, *, description: str = "") -> str:
    desc = html.escape(description or SITE_SUBTITLE)
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)} &middot; {SITE_TITLE}</title>
<meta name="description" content="{desc}">
<meta property="og:title" content="{html.escape(title)}">
<meta property="og:description" content="{desc}">
<style>{CSS}</style>
</head>
<body>
<div class="layout">
<nav class="toc">{nav}</nav>
<main>
{content}
</main>
</div>
<script>{JS}</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Index
# ---------------------------------------------------------------------------


def index_page(pages: list[Page]) -> str:
    chapters = [p for p in pages if p.kind == "chapter"]

    # Group into well-formed sections so the markup nests correctly.
    groups: list[tuple[str | None, list[Page]]] = []
    seen_appendix = False
    for page in pages:
        heading = page.part
        if page.kind == "appendix" and not seen_appendix:
            heading, seen_appendix = "Appendices", True
        if heading or not groups:
            groups.append((heading, []))
        groups[-1][1].append(page)

    toc = []
    for heading, members in groups:
        if heading:
            toc.append(f"<h3>{html.escape(heading)}</h3>")
        toc.append("<ul>")
        for page in members:
            toc.append(f'<li><a href="{page.slug}.html">'
                       f'{html.escape(page.title)}</a></li>')
        toc.append("</ul>")
    body = f"""
<div class="masthead">
  <p class="kicker">Protocol revision 2026-07-28</p>
  <h1>{SITE_TITLE}</h1>
  <p class="lede">{SITE_SUBTITLE}</p>
</div>

<p>This is a protocol book that is secretly a performance book. It teaches the
Model Context Protocol down to the bytes on the wire, then cashes that
understanding out into the three budgets every agentic application spends:
<strong>latency</strong>, <strong>context</strong>, and <strong>cost</strong>.</p>

<p>Every listing comes from <a href="{REPO}">Meridian</a>, the instrumented
reference application that ships with the book. Every number comes from its
measurement harness. Both are reproducible with <code>make bench</code>.</p>

<div class="cards">
  <div class="card"><h3>{len(chapters)} chapters</h3>
    <p>Five parts, from the physics of agent loops to threat modelling.</p></div>
  <div class="card"><h3>134 tests</h3>
    <p>A complete 2026-07-28 implementation with no third-party dependencies.</p></div>
  <div class="card"><h3>Measured, not asserted</h3>
    <p>Nine benchmark scenarios. Model inference is simulated and labelled.</p></div>
  <div class="card"><h3>Runs with Claude Code</h3>
    <p>A dual-era bridge, so it works with clients shipping today.</p></div>
</div>

<h2>Start here</h2>
<ul>
  <li><a href="preface.html">Preface</a>, for why this book exists.</li>
  <li><a href="how-to-read.html">How to read this book</a>, for three routes through.</li>
  <li><a href="ch01.html">Chapter 1</a>, the physics everything else rests on.</li>
</ul>

<h2>Contents</h2>
{"".join(toc)}

<h2>The companion repository</h2>
<pre class="text"><code>git clone {REPO}
cd Building-High-Performance-MCP-Applications

make test      # 134 tests, about two seconds
make bench     # every measurement printed in the book
make book      # build the PDF</code></pre>

<p><a href="{REPO}">Source on GitHub</a>
&middot; <a href="Building-High-Performance-MCP-Applications.pdf">Download the PDF</a></p>
"""
    return shell("Contents", nav_html(pages, "index"), body,
                 description=SITE_SUBTITLE)


# ---------------------------------------------------------------------------


def main() -> int:
    if shutil.which("pandoc") is None:
        print("pandoc is required: brew install pandoc", file=sys.stderr)
        return 1

    pages = structure()
    labels = read_labels()
    home = label_home(pages)
    OUT.mkdir(parents=True, exist_ok=True)

    if not (OUT / "figures").exists():
        print("warning: docs/figures is missing; run `make figures`", file=sys.stderr)

    failures = 0
    for i, page in enumerate(pages):
        try:
            tex = preprocess(page.source.read_text(encoding="utf-8"),
                             labels, home, page.slug)
            body = postprocess(to_html(tex))
            html_out = shell(page.title, nav_html(pages, page.slug),
                             body + pager(pages, i),
                             description=f"{page.title}. {SITE_SUBTITLE}")
            (OUT / f"{page.slug}.html").write_text(html_out, encoding="utf-8")
        except Exception as exc:
            failures += 1
            print(f"  {page.slug:<14} FAILED: {exc}", file=sys.stderr)

    (OUT / "index.html").write_text(index_page(pages), encoding="utf-8")
    (OUT / ".nojekyll").write_text("")

    print(f"{len(pages) - failures}/{len(pages)} pages built into {OUT}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
