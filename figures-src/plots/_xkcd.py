"""Shared setup for the hand-drawn figures.

matplotlib's xkcd mode wants Humor Sans. It is almost never installed, and the
fallback is a crisp sans that ruins the joke. We ship a substitute chosen from
whatever handwriting-ish face the machine actually has, and if there is none we
keep the wobble and accept a plain face, which still reads as a sketch.

Import this from any plot script:

    from _xkcd import sketch, ax_clean, ANNOT
"""

from __future__ import annotations

import contextlib
from pathlib import Path

import matplotlib
import matplotlib.font_manager as fm
import matplotlib.pyplot as plt

# Match book/preamble.tex.
INK = "#15181D"
MUTED = "#5B6472"
RULE = "#C9CED6"
WASH = "#F4F5F7"
ACCENT = "#0F5C8C"
WARM = "#B4531A"
GOOD = "#2C6E49"
DANGER = "#9B2226"

PALETTE = [ACCENT, WARM, GOOD, DANGER, MUTED]

_HANDWRITING_PREFS = [
    "xkcd Script", "Humor Sans", "Comic Neue", "Comic Sans MS",
    "Chalkboard SE", "Chalkboard", "Bradley Hand", "Marker Felt",
]


def _handwriting_family() -> list[str]:
    available = {f.name for f in fm.fontManager.ttflist}
    picked = [n for n in _HANDWRITING_PREFS if n in available]
    return picked + ["DejaVu Sans"]


@contextlib.contextmanager
def sketch(figsize=(7.2, 3.4), dpi=200):
    """Context manager yielding (fig, ax) drawn in hand-sketched style."""
    fams = _handwriting_family()
    with plt.xkcd(scale=1.0, length=100, randomness=2):
        matplotlib.rcParams["font.family"] = fams
        matplotlib.rcParams["font.size"] = 11
        matplotlib.rcParams["path.effects"] = []
        fig, ax = plt.subplots(figsize=figsize, dpi=dpi)
        fig.patch.set_facecolor("white")
        ax.set_facecolor("white")
        yield fig, ax


def ax_clean(ax, xlabel=None, ylabel=None, title=None):
    """Strip an axis down to the two spines xkcd charts actually want."""
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.spines["left"].set_color(INK)
    ax.spines["bottom"].set_color(INK)
    ax.tick_params(colors=INK, labelsize=10)
    if xlabel:
        ax.set_xlabel(xlabel, color=INK)
    if ylabel:
        ax.set_ylabel(ylabel, color=INK)
    if title:
        ax.set_title(title, color=INK, fontsize=12, pad=14)
    return ax


def caption(fig, text, y=-0.02):
    """The dry line underneath, which is where the joke usually lives."""
    fig.text(0.5, y, text, ha="center", va="top", color=MUTED, fontsize=10.5)


ANNOT = dict(
    arrowprops=dict(arrowstyle="->", color=INK, lw=1.4,
                    connectionstyle="arc3,rad=0.22"),
    fontsize=10.5, color=INK,
)
