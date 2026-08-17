"""Chapter 6. What honouring ttlMs is worth on a realistic access pattern.

600 resource reads over 40 distinct URIs with Zipf-ish repetition, which is what
an agent working through a portfolio actually looks like: a few documents read
constantly, a long tail read once.
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
d = data["scenarios"]["cache"]["derived"]

issued = d["readsIssued"]
avoided = d["requestsAvoided"]
served = issued - avoided

fig, (ax, ax2) = plt.subplots(
    1, 2, figsize=(7.6, 2.9), dpi=200, gridspec_kw={"width_ratios": [1.15, 1]}
)
fig.patch.set_facecolor("white")

# --- left: requests that actually reached the server ----------------------
ax.barh([0], [issued], color=WARM, height=0.42, edgecolor="white", linewidth=1.2)
ax.barh([1], [served], color=GOOD, height=0.42, edgecolor="white", linewidth=1.2)
ax.barh([1], [avoided], left=[served], color=GOOD, alpha=0.16,
        height=0.42, edgecolor="white", linewidth=1.2)

ax.text(issued * 0.5, 0, f"{issued} requests", ha="center", va="center",
        color="white", fontsize=10, fontweight="bold")
ax.text(served + 6, 1, f"{served} requests   ({avoided} avoided)",
        va="center", fontsize=9.5, color=INK)

ax.set_yticks([0, 1])
ax.set_yticklabels(["ignoring\nttlMs", "honouring\nttlMs"], fontsize=9.5)
ax.set_xlim(0, issued * 1.12)
ax.set_xlabel(f"requests reaching the server ({issued} reads issued)",
              fontsize=9.5, color=INK)
ax.tick_params(colors=MUTED, labelsize=8.5)
for s in ("top", "right", "left"):
    ax.spines[s].set_visible(False)
ax.spines["bottom"].set_color(RULE)
ax.set_title("Load actually delivered", fontsize=10.5, color=INK, loc="left")

# --- right: the summary numbers -------------------------------------------
ax2.axis("off")
rows = [
    ("hit rate", f"{d['hitRatePct']}%", GOOD),
    ("wall clock", f"{d['uncachedMs']} ms  ->  {d['cachedMs']} ms", ACCENT),
    ("speedup", f"{d['speedup']}x", GOOD),
    ("requests avoided", f"{avoided} of {issued}", ACCENT),
]
ax2.set_xlim(0, 1)
ax2.set_ylim(0, 1)
for i, (label, value, color) in enumerate(rows):
    y = 0.72 - i * 0.20
    ax2.text(0.02, y, label, fontsize=9.5, color=MUTED, va="center")
    ax2.text(0.98, y, value, fontsize=11.5, color=color, va="center",
             ha="right", fontweight="bold")
    ax2.plot([0.02, 0.98], [y - 0.085, y - 0.085], color=RULE, lw=0.7)

ax2.set_title("On a Zipf-ish read pattern", fontsize=10.5, color=INK, loc="left",
              pad=14)

fig.tight_layout()
