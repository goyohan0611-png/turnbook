"""demo/scale.png from research/runs/graph-v9.20-learning/scale_10y-embeddinggemma-300m-ONNX.json:
the share of LoCoMo questions whose answer turn is among the first 10 and 30 found as the
memory grows (the shipped search)."""
from pathlib import Path
import json

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
data = json.loads((ROOT / "research/runs/graph-v9.20-learning"
                   / "scale_10y-embeddinggemma-300m-ONNX.json").read_text(encoding="utf-8"))
sizes = sorted({int(k.split("/")[0]) for k in data if "/" in k})
labels = [f"{s:,} turns" for s in sizes]
fig, ax = plt.subplots(figsize=(8, 4.2), dpi=150)
styles = [("shipped", "hit@30", "answer among the first 30 found", "#2563eb", "-"),
          ("shipped", "hit@10", "answer among the first 10 found", "#60a5fa", "--")]
for arm, metric, name, color, line in styles:
    values = [data[f"{s}/{arm}"][metric] * 100 for s in sizes]
    ax.plot(labels, values, line, color=color, marker="o", linewidth=2.5, label=name)
    for x, y in zip(labels, values):
        ax.annotate(f"{y:.1f}%", (x, y), textcoords="offset points", xytext=(0, 8),
                    ha="center", fontsize=9, color=color)
ax.set_ylim(60, 100)
ax.set_ylabel("answer turn found (%)")
ax.set_title("As the memory grows tenfold, Turnbook still finds the answer", fontsize=11)
ms = [data[f"{s}/shipped"]["ms_median"] for s in sizes]
ax.set_xlabel("memory size  (median search: " + ", ".join(f"{m} ms" for m in ms) + ")")
ax.grid(axis="y", alpha=0.3)
ax.legend(fontsize=8, loc="lower left", frameon=False)
for side in ("top", "right"):
    ax.spines[side].set_visible(False)
fig.text(0.99, 0.01, "LoCoMo, 1,536 questions; EmbeddingGemma; other people's conversations "
         "added", ha="right", fontsize=7, color="#6b7280")
fig.tight_layout()
fig.savefig(ROOT / "demo" / "scale.png")
print("demo/scale.png")
