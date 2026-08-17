"""Chapter 3. Transport overhead, isolated from server execution.

The in-process bar is the control: it still serialises and deserialises, so
subtracting it leaves transport cost and nothing else. The point of the chart
is how small all three are next to a single network hop, which is why the
right-hand annotation exists.
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
rows = data["scenarios"]["transport"]["results"]

labels = [r["label"] for r in rows]
means = [r["meanMs"] for r in rows]
p95 = [r["p95Ms"] for r in rows]
control = means[0]

fig, (ax, ax2) = plt.subplots(
    1, 2, figsize=(7.6, 3.0), dpi=200, gridspec_kw={"width_ratios": [1.35, 1]}
)
fig.patch.set_facecolor("white")

# --- left: the three transports -------------------------------------------
colors = [MUTED, GOOD, ACCENT]
y = np.arange(len(labels))
ax.barh(y, means, color=colors, height=0.55, edgecolor="white", linewidth=1.0)
ax.errorbar(means, y, xerr=[[0] * len(means), [p - m for p, m in zip(p95, means)]],
            fmt="none", ecolor=INK, elinewidth=1.0, capsize=3, alpha=0.55)

for i, (m, label) in enumerate(zip(means, labels)):
    over = "" if i == 0 else f"   (+{m - control:.3f} over control)"
    ax.text(m + 0.006, i, f"{m:.3f} ms{over}", va="center", fontsize=9, color=INK)

ax.set_yticks(y)
ax.set_yticklabels(labels, fontsize=9.5)
ax.invert_yaxis()
ax.set_xlim(0, max(p95) * 1.85)
ax.set_xlabel("milliseconds per call (loopback)", fontsize=9.5, color=INK)
ax.tick_params(colors=MUTED, labelsize=8.5)
for spine in ("top", "right", "left"):
    ax.spines[spine].set_visible(False)
ax.spines["bottom"].set_color(RULE)
ax.set_title("Protocol overhead, isolated", fontsize=10.5, color=INK, loc="left")

# --- right: the same bars next to one real network hop --------------------
hops = [("loopback\nHTTP", means[2], ACCENT),
        ("same-region\nRTT", 1.2, MUTED),
        ("cross-region\nRTT", 68.0, WARM)]
x = np.arange(len(hops))
ax2.bar(x, [h[1] for h in hops], color=[h[2] for h in hops],
        width=0.58, edgecolor="white", linewidth=1.0)
ax2.set_yscale("log")
ax2.set_xticks(x)
ax2.set_xticklabels([h[0] for h in hops], fontsize=9)
ax2.set_ylabel("ms (log scale)", fontsize=9.5, color=INK)
ax2.tick_params(colors=MUTED, labelsize=8.5)
for spine in ("top", "right"):
    ax2.spines[spine].set_visible(False)
for spine in ("left", "bottom"):
    ax2.spines[spine].set_color(RULE)
for xi, (_, v, _) in zip(x, hops):
    ax2.text(xi, v * 1.35, f"{v:g}", ha="center", fontsize=9, color=INK)
ax2.set_title("...next to actual distance", fontsize=10.5, color=INK, loc="left")

fig.tight_layout()
