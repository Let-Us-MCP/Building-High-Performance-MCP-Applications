"""Chapter 18 cartoon: a shared TTL synchronises the fleet you built."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _xkcd import ACCENT, DANGER, GOOD, INK, MUTED, ax_clean, caption, sketch

# Same 2,000 clients, same one-hour TTL. Left: everybody expires together.
MINUTES = list(range(0, 60, 2))
LOCKSTEP = [4] * 29 + [1850]
JITTERED = [4] * 29 + [4]
# spread the jittered fleet across the last ten minutes instead
for i, m in enumerate(MINUTES):
    if m >= 40:
        JITTERED[i] = 150

with sketch(figsize=(7.4, 3.9)) as (fig, ax):
    ax.bar([m - 0.45 for m in MINUTES], LOCKSTEP, width=1.5,
           color=DANGER, edgecolor=INK, linewidth=0.9, label="one shared TTL")
    ax.bar([m + 0.95 for m in MINUTES], JITTERED, width=1.5,
           color=GOOD, edgecolor=INK, linewidth=0.9, label="TTL with full jitter")

    ax.set_ylim(0, 2100)
    ax_clean(ax, xlabel="minutes past the hour", ylabel="requests arriving")
    ax.legend(frameon=False, fontsize=9.5, loc="upper left")

    ax.annotate(
        "2,000 clients,\nthe same second",
        xy=(58, 1850), xytext=(41, 1650),
        arrowprops=dict(arrowstyle="->", color=DANGER, lw=1.5,
                        connectionstyle="arc3,rad=0.25"),
        fontsize=10, color=DANGER, ha="center",
    )

    caption(fig, "You did not get a traffic spike. You built a metronome.",
            y=-0.13)
