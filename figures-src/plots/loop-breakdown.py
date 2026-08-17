"""Chapter 1. Where a task's wall clock actually goes.

Data from meridian/bench/results.json, scenario `loop`: a three-iteration
agent loop against four servers. The point of the chart is the proportion,
which is why the transport slice is annotated rather than left to be squinted at.
"""

import json
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

sys.path.insert(0, str(Path(__file__).parent))
from _xkcd import ACCENT, GOOD, INK, MUTED, RULE, WARM, WASH

RESULTS = Path(__file__).resolve().parents[2] / "meridian" / "bench" / "results.json"
data = json.loads(RESULTS.read_text())
loop = data["scenarios"]["loop"]["results"][0]

model_ms = loop["modelMs"]
transport_ms = loop["transportMs"]
total_ms = loop["wallMs"]

fig, ax = plt.subplots(figsize=(7.4, 2.5), dpi=200)
fig.patch.set_facecolor("white")
ax.set_facecolor("white")

segments = [
    ("model inference", model_ms, ACCENT),
    ("transport + server execution", transport_ms, GOOD),
]

left = 0.0
for label, width, color in segments:
    ax.barh([0], [width], left=left, color=color, height=0.5,
            edgecolor="white", linewidth=1.2)
    pct = 100.0 * width / total_ms
    if pct > 12:
        ax.text(left + width / 2, 0, f"{label}\n{width:,.0f} ms  ({pct:.1f}%)",
                ha="center", va="center", color="white", fontsize=10,
                fontweight="bold")
    else:
        ax.annotate(
            f"{label}\n{width:,.1f} ms  ({pct:.1f}%)",
            xy=(left + width / 2, 0.26), xytext=(left + width / 2, 0.72),
            ha="center", va="bottom", fontsize=9.5, color=GOOD,
            arrowprops=dict(arrowstyle="-", color=GOOD, lw=1.0),
        )
    left += width

ax.set_xlim(0, total_ms)
ax.set_ylim(-0.55, 1.05)
ax.set_yticks([])
ax.set_xlabel("milliseconds of one task", color=INK, fontsize=10)
ax.tick_params(colors=MUTED, labelsize=9)
for spine in ("top", "right", "left"):
    ax.spines[spine].set_visible(False)
ax.spines["bottom"].set_color(RULE)

ax.text(0, -0.45, f"total {total_ms:,.0f} ms   ·   3 model turns, 3 tool calls, "
                  f"4 servers", color=MUTED, fontsize=9.5, va="top")
