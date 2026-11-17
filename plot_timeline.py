"""Draw results/first_block_timeline.png from results/first_block_timeline.csv."""
import csv
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

rows = {r["half_year"]: int(r["total"]) for r in csv.DictReader(open("results/first_block_timeline.csv"))}
first, last = min(rows), max(rows)
halves, (y, h) = [], (int(first[:4]), int(first[-1]))
while f"{y}-H{h}" <= last:
    halves.append(f"{y}-H{h}")
    y, h = (y, 2) if h == 1 else (y + 1, 1)
counts = [rows.get(k, 0) for k in halves]

fig, ax = plt.subplots(figsize=(9, 3.6), dpi=160)
fig.patch.set_facecolor("#fcfcfb"); ax.set_facecolor("#fcfcfb")
bars = ax.bar(halves, counts, width=0.62, color="#2a78d6")
for b, c in zip(bars, counts):
    if c: ax.text(b.get_x() + b.get_width() / 2, c + 0.15, str(c), ha="center", va="bottom", fontsize=10, color="#0b0b0b")
ax.set_title("Sites blocking an AI crawler for the first time, by half-year", loc="left", fontsize=12, color="#0b0b0b", pad=12)
ax.set_ylim(0, max(counts) + 1.5); ax.set_yticks([])
for s in ("top", "right", "left"): ax.spines[s].set_visible(False)
ax.spines["bottom"].set_color("#c9c8c4")
ax.tick_params(axis="x", colors="#52514e", labelsize=8.5, length=0)
fig.tight_layout()
fig.savefig("results/first_block_timeline.png", facecolor=fig.get_facecolor())
