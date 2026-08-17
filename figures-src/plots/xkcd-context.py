"""Chapter 1 cartoon: what actually occupies the context window."""

import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
from _xkcd import ACCENT, DANGER, GOOD, INK, MUTED, WARM, caption, sketch

LABELS = [
    "tool descriptions\nfor tools never called",
    "a JSON blob the model\nread one field from",
    "output of step 2,\nnow irrelevant",
    "the system prompt,\nagain",
    "what the user\nactually asked",
]
SHARE = [34, 27, 21, 13, 5]
COLORS = [MUTED, MUTED, MUTED, MUTED, GOOD]

with sketch(figsize=(7.6, 3.6)) as (fig, ax):
    left = 0
    for label, share, color in zip(LABELS, SHARE, COLORS):
        ax.barh([0], [share], left=left, color=color, height=0.55,
                edgecolor=INK, linewidth=1.8)
        left += share

    ax.set_xlim(0, 100)
    ax.set_ylim(-1.9, 1.9)
    ax.set_yticks([])
    ax.set_xticks([])
    for spine in ax.spines.values():
        spine.set_visible(False)

    # Annotate above and below alternately so the labels do not collide.
    positions, left = [], 0
    for share in SHARE:
        positions.append(left + share / 2)
        left += share

    for i, (label, pos) in enumerate(zip(LABELS, positions)):
        above = i % 2 == 0
        y_text = 1.55 if above else -1.55
        color = GOOD if i == len(LABELS) - 1 else INK
        ax.annotate(
            label,
            xy=(pos, 0.30 if above else -0.30),
            xytext=(pos, y_text),
            ha="center", va="bottom" if above else "top",
            fontsize=9.5, color=color,
            arrowprops=dict(arrowstyle="->", color=color, lw=1.3,
                            connectionstyle="arc3,rad=0.0"),
        )

    ax.text(50, 0, "YOUR CONTEXT WINDOW", ha="center", va="center",
            color="white", fontsize=12, fontweight="bold")

    caption(fig, "You are billed for all of it. Every single turn.", y=-0.02)
