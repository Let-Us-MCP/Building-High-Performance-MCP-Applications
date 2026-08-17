"""The cover.

Abstract, generated, and therefore copyright-clean by construction: it is our
own work and the source ships beside it. No stock library, no attribution
question, no licence to re-check before the second printing.

The image is a message-flow field. Requests leave a host on the left and arrive
at servers on the right, and the density band across the middle is what a
saturated agent loop actually looks like when you plot it. It is decorative,
but it is decorative about the right thing.
"""

import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from matplotlib.patches import Circle, FancyArrowPatch

sys.path.insert(0, str(Path(__file__).parent))

INK = "#0B0E13"
ACCENT = "#0F5C8C"
ACCENT_LIGHT = "#4FA3D1"
WARM = "#B4531A"
GOOD = "#2C6E49"
PAPER = "#F7F5F0"

rng = np.random.default_rng(20260728)

# 7.5 x 9.25 inches, matching the trim size in book/preamble.tex.
fig, ax = plt.subplots(figsize=(7.5, 9.25), dpi=300)
fig.patch.set_facecolor(INK)
ax.set_facecolor(INK)
ax.set_xlim(0, 1)
ax.set_ylim(0, 1)
ax.axis("off")

# --- the flow field --------------------------------------------------------
# Each arc is one request crossing from host to server. Their envelope is a
# lognormal-ish latency distribution, which is why the band is dense low and
# has a long thin tail high.

HOSTS = np.linspace(0.40, 0.86, 5)      # a few clients, clustered
SERVERS = np.linspace(0.34, 0.94, 11)   # more servers, spread wider

X0, X1 = 0.10, 0.90
N = 190

for i in range(N):
    # Every arc starts at a real host and ends at a real server, so the
    # bundles converge instead of smearing.
    y0 = rng.choice(HOSTS) + rng.normal(0, 0.006)
    y1 = rng.choice(SERVERS) + rng.normal(0, 0.008)

    t = np.linspace(0, 1, 260)
    # Two control points, bowed away from the straight line, so bundles
    # separate in the middle and rejoin at the endpoints.
    bow = rng.normal(0, 0.075) + np.sign(y1 - y0 + 1e-9) * 0.035
    c1x, c1y = X0 + 0.30, y0 + bow * 0.7
    c2x, c2y = X1 - 0.30, y1 + bow * 0.7

    x = ((1 - t) ** 3 * X0 + 3 * (1 - t) ** 2 * t * c1x
         + 3 * (1 - t) * t ** 2 * c2x + t ** 3 * X1)
    y = ((1 - t) ** 3 * y0 + 3 * (1 - t) ** 2 * t * c1y
         + 3 * (1 - t) * t ** 2 * c2y + t ** 3 * y1)

    depth = rng.random()
    if depth > 0.955:
        color, lw, alpha, z = WARM, 1.6, 0.9, 14        # the slow tail
    elif depth > 0.88:
        color, lw, alpha, z = GOOD, 1.25, 0.75, 12      # the cache hits
    elif depth > 0.52:
        color, lw, alpha, z = ACCENT_LIGHT, 0.75, 0.42, 8
    else:
        color, lw, alpha, z = ACCENT, 0.55, 0.22, 4

    ax.plot(x, y, color=color, lw=lw, alpha=alpha, solid_capstyle="round", zorder=z)

# --- endpoints -------------------------------------------------------------
for y in HOSTS:
    ax.add_patch(Circle((X0, y), 0.010, color=PAPER, alpha=0.95, zorder=20))
    ax.add_patch(Circle((X0, y), 0.019, color=PAPER, alpha=0.12, zorder=19))
for y in SERVERS:
    ax.add_patch(Circle((X1, y), 0.0065, color=ACCENT_LIGHT, alpha=0.95, zorder=20))

# --- title plate -----------------------------------------------------------
ax.add_patch(plt.Rectangle((0.0, 0.0), 1.0, 0.30, color=INK, alpha=0.93, zorder=30))
ax.plot([0.09, 0.42], [0.30, 0.30], color=ACCENT_LIGHT, lw=1.6, alpha=0.9, zorder=31)

ax.text(0.09, 0.225, "Building Awesome", color=PAPER, fontsize=33,
        fontweight="bold", va="center", ha="left", zorder=32,
        family="DejaVu Serif")
ax.text(0.09, 0.163, "MCP Apps", color=PAPER, fontsize=33,
        fontweight="bold", va="center", ha="left", zorder=32,
        family="DejaVu Serif")

ax.text(0.09, 0.108,
        "What every AI application developer should know",
        color="#9AA6B2", fontsize=11.5, va="center", ha="left", zorder=32)
ax.text(0.09, 0.083,
        "about the Model Context Protocol",
        color="#9AA6B2", fontsize=11.5, va="center", ha="left", zorder=32)

ax.text(0.09, 0.035, "KRIMLER", color=PAPER, fontsize=13.5,
        va="center", ha="left", zorder=32, fontweight="bold")
ax.text(0.91, 0.035, "revision 2026-07-28", color=ACCENT_LIGHT, fontsize=10,
        va="center", ha="right", zorder=32, family="monospace")

ax.set_position([0, 0, 1, 1])
