"""Chapter 13 cartoon: input climbs every turn, output never does."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _xkcd import ACCENT, GOOD, INK, MUTED, WARM, ax_clean, caption, sketch

TURNS = [1, 2, 3, 4, 5, 6]
INPUT = [1252, 1318, 1400, 1642, 1961, 2380]
OUTPUT = [12, 19, 8, 14, 11, 9]

with sketch(figsize=(7.4, 3.9)) as (fig, ax):
    ax.plot(TURNS, INPUT, color=ACCENT, lw=2.6, marker="o", ms=6,
            label="input tokens (what you re-read)")
    ax.plot(TURNS, OUTPUT, color=GOOD, lw=2.6, marker="o", ms=6,
            label="output tokens (what you asked for)")

    ax.set_ylim(0, 2750)
    ax.set_xlim(0.7, 6.5)
    ax_clean(ax, xlabel="loop iteration", ylabel="tokens")
    ax.legend(frameon=False, fontsize=9.5, loc="upper left")

    ax.annotate(
        "every tool result\nstays in here",
        xy=(5.0, 1961), xytext=(3.1, 2350),
        arrowprops=dict(arrowstyle="->", color=WARM, lw=1.5,
                        connectionstyle="arc3,rad=-0.25"),
        fontsize=10, color=WARM, ha="center",
    )
    ax.annotate(
        "still just a tool call",
        xy=(5.0, 11), xytext=(4.6, 430),
        arrowprops=dict(arrowstyle="->", color=MUTED, lw=1.4,
                        connectionstyle="arc3,rad=0.3"),
        fontsize=10, color=MUTED, ha="center",
    )

    caption(fig, "Your bill is dominated by re-reading what you already read.",
            y=-0.13)
