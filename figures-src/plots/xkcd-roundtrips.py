"""Chapter 1 cartoon: everybody optimises the wrong thing."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _xkcd import ACCENT, DANGER, GOOD, INK, MUTED, WARM, ax_clean, caption, sketch

LABELS = [
    "rewrote the\nserver in Rust",
    "shaved 40 KB\noff the JSON",
    "switched to a\nfaster JSON parser",
    "removed one\nround trip",
]
SAVED = [3, 2, 1, 610]
COLORS = [MUTED, MUTED, MUTED, GOOD]

with sketch(figsize=(7.4, 3.9)) as (fig, ax):
    bars = ax.bar(range(len(LABELS)), SAVED, color=COLORS, width=0.62,
                  edgecolor=INK, linewidth=1.6)
    ax.set_xticks(range(len(LABELS)))
    ax.set_xticklabels(LABELS, fontsize=9.5)
    ax.set_ylim(0, 760)
    ax_clean(ax, ylabel="milliseconds saved")

    for i, v in enumerate(SAVED):
        ax.text(i, v + 22, f"{v} ms", ha="center", color=INK, fontsize=10.5)

    ax.annotate(
        "three weeks",
        xy=(0.0, 60), xytext=(0.15, 300),
        arrowprops=dict(arrowstyle="->", color=WARM, lw=1.5,
                        connectionstyle="arc3,rad=-0.3"),
        fontsize=10.5, color=WARM, ha="center",
    )
    ax.annotate(
        "one afternoon",
        xy=(3.0, 640), xytext=(2.05, 700),
        arrowprops=dict(arrowstyle="->", color=GOOD, lw=1.5,
                        connectionstyle="arc3,rad=0.25"),
        fontsize=10.5, color=GOOD, ha="center",
    )

    caption(fig, "Round trips are the enemy. Everything else is rounding error.",
            y=-0.13)
