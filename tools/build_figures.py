#!/usr/bin/env python3
"""Build every figure to both PDF (for LaTeX) and SVG (for the website).

Two kinds of source live under `figures-src/`:

  tikz/*.tex   standalone TikZ pictures. Compiled with pdflatex, then converted
               to SVG with dvisvgm's PDF mode.
  plots/*.py   matplotlib scripts. Each defines `draw(fig)` or just plots at
               import time; the harness saves both formats.

Output lands in `book/figures/<name>.pdf` and `docs/figures/<name>.svg`.

Usage:
    python3 tools/build_figures.py            # build what changed
    python3 tools/build_figures.py --force    # rebuild everything
    python3 tools/build_figures.py cover loop-waterfall
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TIKZ_SRC = ROOT / "figures-src" / "tikz"
PLOT_SRC = ROOT / "figures-src" / "plots"
PDF_OUT = ROOT / "book" / "figures"
SVG_OUT = ROOT / "docs" / "figures"
VENV_PY = ROOT / ".venv" / "bin" / "python"


def python_bin() -> str:
    return str(VENV_PY) if VENV_PY.exists() else sys.executable


def newer(src: Path, *targets: Path) -> bool:
    if not all(t.exists() for t in targets):
        return True
    return src.stat().st_mtime > min(t.stat().st_mtime for t in targets)


def run(cmd: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)


def build_tikz(src: Path, force: bool) -> tuple[str, bool, str]:
    name = src.stem
    pdf, svg = PDF_OUT / f"{name}.pdf", SVG_OUT / f"{name}.svg"
    if not force and not newer(src, pdf, svg):
        return name, True, "up to date"

    with tempfile.TemporaryDirectory() as tmp:
        # Copy the whole source dir so figures can share a `_style.tex`.
        tmpdir = Path(tmp) / "tikz"
        shutil.copytree(TIKZ_SRC, tmpdir)
        proc = run(
            ["pdflatex", "-interaction=nonstopmode", "-halt-on-error", f"{name}.tex"],
            cwd=tmpdir,
        )
        built = tmpdir / f"{name}.pdf"
        if not built.exists():
            tail = "\n".join(
                l for l in proc.stdout.splitlines() if l.startswith("!") or "Error" in l
            )
            return name, False, f"pdflatex failed\n{tail[:900]}"

        PDF_OUT.mkdir(parents=True, exist_ok=True)
        SVG_OUT.mkdir(parents=True, exist_ok=True)
        shutil.copy(built, pdf)

        err = pdf_to_svg(built, svg)
        if err:
            return name, False, err

    return name, True, "built"


def pdf_to_svg(pdf: Path, svg: Path) -> str | None:
    """Convert a one-page PDF to SVG. Returns an error string, or None on success.

    dvisvgm gives smaller output and keeps text as text, but its PDF mode needs
    mutool, which is not always installed. pdftocairo (poppler) always works and
    outlines the glyphs, which is fine for the web since it removes the font
    dependency entirely.
    """
    if shutil.which("dvisvgm"):
        proc = run(["dvisvgm", "--pdf", "--font-format=woff", "--exact-bbox",
                    "--output=" + str(svg), str(pdf)])
        if svg.exists() and svg.stat().st_size > 0:
            return None
    if shutil.which("pdftocairo"):
        proc = run(["pdftocairo", "-svg", str(pdf), str(svg)])
        if svg.exists() and svg.stat().st_size > 0:
            return None
        return f"pdftocairo failed\n{proc.stderr[-700:]}"
    return "no SVG converter found (install dvisvgm with mutool, or poppler)"


PLOT_HARNESS = r"""
import sys, pathlib, runpy
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

src, pdf_out, svg_out = sys.argv[1], sys.argv[2], sys.argv[3]
runpy.run_path(src, run_name="__figure__")

figs = [plt.figure(n) for n in plt.get_fignums()]
if not figs:
    raise SystemExit("figure script produced no figure: " + src)
fig = figs[-1]
for path in (pdf_out, svg_out):
    pathlib.Path(path).parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, bbox_inches="tight", pad_inches=0.06, transparent=False)
"""


def build_plot(src: Path, force: bool) -> tuple[str, bool, str]:
    name = src.stem
    pdf, svg = PDF_OUT / f"{name}.pdf", SVG_OUT / f"{name}.svg"
    if not force and not newer(src, pdf, svg):
        return name, True, "up to date"

    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as fh:
        fh.write(PLOT_HARNESS)
        harness = fh.name
    try:
        proc = run([python_bin(), harness, str(src), str(pdf), str(svg)])
    finally:
        os.unlink(harness)

    if proc.returncode != 0 or not pdf.exists():
        return name, False, (proc.stderr or proc.stdout)[-900:]
    return name, True, "built"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("names", nargs="*", help="figure basenames to build")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    # A leading underscore marks a shared include, not a figure of its own.
    sources: list[tuple[Path, str]] = []
    sources += [(p, "tikz") for p in sorted(TIKZ_SRC.glob("*.tex")) if p.stem[0] != "_"]
    sources += [(p, "plot") for p in sorted(PLOT_SRC.glob("*.py")) if p.stem[0] != "_"]
    if args.names:
        wanted = set(args.names)
        sources = [(p, k) for p, k in sources if p.stem in wanted]

    if not sources:
        print("no figure sources found")
        return 0

    failures = 0
    for src, kind in sources:
        name, ok, msg = (build_tikz if kind == "tikz" else build_plot)(src, args.force)
        if ok:
            if msg != "up to date":
                print(f"  {name:<34} {msg}")
        else:
            failures += 1
            print(f"  {name:<34} FAILED\n{msg}\n", file=sys.stderr)

    total = len(sources)
    print(f"{total - failures}/{total} figures OK")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
