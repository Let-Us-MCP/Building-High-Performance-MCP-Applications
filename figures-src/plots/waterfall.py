"""Chapter 16. The instrumented waterfall for one Meridian task.

Data from meridian/bench/results.json, scenario `loop`, serial variant. Each
iteration is decomposed into the bands from Chapter 1, drawn to scale, with the
span hierarchy on the left the way a tracing UI would show it.
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
steps = loop["steps"]

# Build the span list: for each iteration, a model band then a transport band.
rows = []
t = 0.0
for s in steps:
    i = s["i"]
    rows.append((f"iteration {i}", None, t, s["totalMs"], None))
    rows.append((f"    model turn", "model", t, s["modelMs"], ACCENT))
    t += s["modelMs"]
    if s["transportMs"] > 0:
        n = s["toolCalls"]
        label = f"    {n} tool call{'s' if n != 1 else ''}"
        if s["parallel"]:
            label += " (parallel)"
        rows.append((label, "tool", t, s["transportMs"], GOOD))
        t += s["transportMs"]

total = t

fig, ax = plt.subplots(figsize=(7.6, 3.9), dpi=200)
fig.patch.set_facecolor("white")
ax.set_facecolor("white")

y = 0
labels, ypos = [], []
for label, kind, start, width, color in rows:
    if kind is None:
        ax.barh([y], [width], left=[start], color=WASH, height=0.62,
                edgecolor=RULE, linewidth=0.9)
        ax.text(start + width + 8, y, f"{width:,.0f} ms", va="center",
                fontsize=8.5, color=MUTED)
    else:
        ax.barh([y], [width], left=[start], color=color, height=0.46,
                edgecolor="white", linewidth=0.8)
        if width > total * 0.06:
            ax.text(start + width / 2, y, f"{width:,.0f}", va="center",
                    ha="center", fontsize=8.5, color="white", fontweight="bold")
        else:
            ax.text(start + width + 8, y, f"{width:,.1f} ms", va="center",
                    fontsize=8.5, color=GOOD)
    labels.append(label)
    ypos.append(y)
    y -= 1

ax.set_yticks(ypos)
ax.set_yticklabels(labels, fontsize=8.5, family="monospace")
ax.tick_params(axis="y", length=0, colors=INK)
ax.tick_params(axis="x", colors=MUTED, labelsize=8.5)
ax.set_xlim(0, total * 1.16)
ax.set_ylim(y + 0.5, 0.8)
ax.set_xlabel("milliseconds from start of task", fontsize=9.5, color=INK)
for s in ("top", "right", "left"):
    ax.spines[s].set_visible(False)
ax.spines["bottom"].set_color(RULE)
ax.grid(axis="x", color=RULE, lw=0.5, alpha=0.6)
ax.set_axisbelow(True)

ax.set_title(
    f"One Meridian task: {total:,.0f} ms, "
    f"{loop['modelSharePct']}% of it model inference",
    fontsize=10.5, color=INK, loc="left", pad=10)

fig.tight_layout()
