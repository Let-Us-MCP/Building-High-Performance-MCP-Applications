"""Chapter 8 cartoon: the true cost of asking a question mid-call."""

import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
from _xkcd import ACCENT, DANGER, GOOD, INK, MUTED, WARM, caption, sketch

# From the mrtr scenario in meridian/bench/results.json, plus one model turn.
PARTS = [
    ("what you think\nan elicitation costs", 34.5, MUTED),
    ("the model turn it\ndrags along with it", 340.0, WARM),
]

with sketch(figsize=(7.4, 3.5)) as (fig, ax):
    left = 0
    for label, width, color in PARTS:
        ax.barh([0], [width], left=left, color=color, height=0.5,
                edgecolor=INK, linewidth=1.8)
        left += width

    ax.set_xlim(0, 420)
    ax.set_ylim(-1.6, 1.9)
    ax.set_yticks([])
    ax.set_xticks([0, 100, 200, 300, 400])
    ax.set_xlabel("milliseconds", color=INK, fontsize=10.5)
    for spine in ("top", "right", "left"):
        ax.spines[spine].set_visible(False)
    ax.spines["bottom"].set_color(INK)
    ax.tick_params(colors=INK, labelsize=10)

    ax.annotate(PARTS[0][0], xy=(17, 0.28), xytext=(30, 1.45),
                ha="center", fontsize=10, color=INK,
                arrowprops=dict(arrowstyle="->", color=INK, lw=1.3))
    ax.annotate(PARTS[1][0], xy=(205, -0.28), xytext=(215, -1.15),
                ha="center", fontsize=10, color=WARM,
                arrowprops=dict(arrowstyle="->", color=WARM, lw=1.3))

    ax.text(390, 0, "375 ms", va="center", ha="right", fontsize=11.5,
            color=INK, fontweight="bold")

    caption(fig, "A round trip is never just a round trip.", y=0.02)
