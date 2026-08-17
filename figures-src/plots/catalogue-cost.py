"""Chapter 5. What a tool catalogue costs, and why it is a recurring bill.

Left: the two catalogues, in tokens. Right: the same difference accumulated
across turns, which is the framing that changes people's minds. A catalogue is
not a startup cost.
"""

import json
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
from _xkcd import ACCENT, DANGER, GOOD, INK, MUTED, RULE, WARM

RESULTS = Path(__file__).resolve().parents[2] / "meridian" / "bench" / "results.json"
data = json.loads(RESULTS.read_text())
cat = data["scenarios"]["catalogue"]
slim, fat = cat["results"][0], cat["results"][1]
INPUT_USD_PER_MTOK = 3.00

fig, (ax, ax2) = plt.subplots(
    1, 2, figsize=(7.6, 3.1), dpi=200, gridspec_kw={"width_ratios": [1, 1.25]}
)
fig.patch.set_facecolor("white")

# --- left: catalogue size --------------------------------------------------
names = [f"{slim['tools']} task-shaped\ntools", f"{fat['tools']} REST-mirror\ntools"]
values = [slim["tokens"], fat["tokens"]]
bars = ax.bar([0, 1], values, color=[GOOD, WARM], width=0.55,
              edgecolor="white", linewidth=1.2)
for x, v in zip([0, 1], values):
    ax.text(x, v + 130, f"{v:,}", ha="center", fontsize=10.5, color=INK,
            fontweight="bold")
ax.set_xticks([0, 1])
ax.set_xticklabels(names, fontsize=9.5)
ax.set_ylabel("tokens, every turn", fontsize=9.5, color=INK)
ax.set_ylim(0, max(values) * 1.22)
ax.tick_params(colors=MUTED, labelsize=8.5)
for s in ("top", "right"):
    ax.spines[s].set_visible(False)
for s in ("left", "bottom"):
    ax.spines[s].set_color(RULE)
ax.set_title("Catalogue size", fontsize=10.5, color=INK, loc="left")

# --- right: accumulated across turns --------------------------------------
turns = np.arange(0, 13)
slim_cum = slim["tokens"] * turns
fat_cum = fat["tokens"] * turns

ax2.fill_between(turns, slim_cum, fat_cum, color=WARM, alpha=0.16,
                 label="pure waste")
ax2.plot(turns, fat_cum, color=WARM, lw=2.0, label=f"{fat['tools']} tools")
ax2.plot(turns, slim_cum, color=GOOD, lw=2.0, label=f"{slim['tools']} tools")

ax2.set_xlabel("model turns in one task", fontsize=9.5, color=INK)
ax2.set_ylabel("cumulative input tokens", fontsize=9.5, color=INK)
ax2.tick_params(colors=MUTED, labelsize=8.5)
for s in ("top", "right"):
    ax2.spines[s].set_visible(False)
for s in ("left", "bottom"):
    ax2.spines[s].set_color(RULE)
ax2.set_ylim(0, fat_cum[-1] * 1.05)
ax2.legend(frameon=False, fontsize=9, loc="upper left")
ax2.set_title("The same difference, accumulating", fontsize=10.5, color=INK,
              loc="left")

delta = (fat["tokens"] - slim["tokens"]) * 8
ax2.annotate(
    f"at 8 turns: {delta:,} wasted tokens\n"
    f"${delta * INPUT_USD_PER_MTOK / 1e6 * 1000:,.0f} per 1,000 tasks",
    xy=(8, (fat_cum[8] + slim_cum[8]) / 2),
    xytext=(0.5, fat_cum[-1] * 0.30),
    fontsize=9, color=WARM,
    arrowprops=dict(arrowstyle="->", color=WARM, lw=1.2,
                    connectionstyle="arc3,rad=0.25"),
)

fig.tight_layout()
