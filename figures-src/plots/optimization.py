"""Chapter 17. The optimisation pass, attributed one change at a time.

Two panels because the two budgets move at different points. Latency falls when
iterations fall. Tokens fall when the catalogue shrinks. Neither change helps
the other, which is exactly why the attribution matters.
"""

import json
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
from _xkcd import ACCENT, GOOD, INK, MUTED, RULE, WARM

RESULTS = Path(__file__).resolve().parents[2] / "meridian" / "bench" / "results.json"
data = json.loads(RESULTS.read_text())
rows = data["scenarios"]["optimization"]["results"]

labels = [r["label"] for r in rows]
wall = [r["wallMs"] for r in rows]
tokens = [r["tokens"] for r in rows]

wrapped = [l.replace(" the ", "\nthe ").replace(" into ", "\ninto ")
            .replace(" out ", " out\n") for l in labels]

fig, (ax, ax2) = plt.subplots(1, 2, figsize=(7.7, 3.4), dpi=200)
fig.patch.set_facecolor("white")

x = np.arange(len(rows))


def step_panel(axis, values, color, ylabel, fmt, title):
    # Colour a bar green only when it actually improved on its predecessor.
    colors = [MUTED]
    for i in range(1, len(values)):
        colors.append(color if values[i] < values[i - 1] * 0.98 else RULE)
    axis.bar(x, values, color=colors, width=0.62, edgecolor="white", linewidth=1.0)
    for xi, v in zip(x, values):
        axis.text(xi, v + max(values) * 0.03, fmt.format(v), ha="center",
                  fontsize=8.5, color=INK)
    # Connect the tops so the staircase is visible.
    axis.plot(x, values, color=INK, lw=0.9, alpha=0.35, marker="o",
              markersize=3, zorder=5)
    axis.set_xticks(x)
    axis.set_xticklabels(wrapped, fontsize=7.8)
    axis.set_ylabel(ylabel, fontsize=9.5, color=INK)
    axis.set_ylim(0, max(values) * 1.20)
    axis.tick_params(colors=MUTED, labelsize=8)
    for s in ("top", "right"):
        axis.spines[s].set_visible(False)
    for s in ("left", "bottom"):
        axis.spines[s].set_color(RULE)
    axis.set_title(title, fontsize=10.5, color=INK, loc="left")


step_panel(ax, wall, ACCENT, "wall clock (ms)", "{:,.0f}",
           "Latency falls when iterations do")
step_panel(ax2, tokens, GOOD, "tokens per task", "{:,.0f}",
           "Tokens fall when the catalogue does")

fig.tight_layout()
