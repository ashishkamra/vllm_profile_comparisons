# SPDX-License-Identifier: Apache-2.0
"""Render comparison figures from analyze_trace.py CSVs.

    plot_compare.py results/*-stages.csv --out figures

Three figures, because "where did the time go" has three honest answers:
  fig1-stages.png      engine stage shares per run           (coarse, comparable)
  fig2-op-classes.png  op class shares per run               (what kind of math)
  fig3-linear.png      linear sub-stage split                (which projection)
"""

from __future__ import annotations

import argparse
import collections
import csv
import os

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

STAGE_ORDER = [
    "frontend.tokenize",
    "frontend.other",
    "engine.schedule",
    "engine.kv_cache",
    "engine.postprocess",
    "runner.prepare_inputs",
    "model.forward",
    "model.lm_head",
    "sample",
    "frontend.detokenize",
    "other",
]

OP_ORDER = [
    "linear.qkv",
    "linear.o_proj",
    "linear.gate_up",
    "linear.down",
    "linear.lm_head",
    "linear",
    "attention.paged",
    "attention.kv_write",
    "norm",
    "rope",
    "activation",
    "elementwise",
    "sampler",
    "python",
]

METRIC = {"cpu": "self_ms", "gpu": "device_ms"}


def stacked(runs: dict[str, dict[str, float]], order: list[str], title: str,
            xlabel: str, out: str) -> None:
    tags = sorted(runs)
    if not tags:
        return
    keys = [k for k in order if any(k in runs[t] for t in tags)]
    keys += sorted({k for t in tags for k in runs[t]} - set(keys))

    fig, ax = plt.subplots(figsize=(9, 0.9 * len(tags) + 2.2))
    left = [0.0] * len(tags)
    cmap = plt.get_cmap("tab20")
    for i, key in enumerate(keys):
        vals = [runs[t].get(key, 0.0) for t in tags]
        ax.barh(tags, vals, left=left, label=key, color=cmap(i % 20))
        left = [l + v for l, v in zip(left, vals)]

    ax.set_xlim(0, 100)
    ax.set_xlabel(xlabel)
    ax.set_title(title)
    ax.invert_yaxis()
    ax.legend(bbox_to_anchor=(1.01, 1), loc="upper left", fontsize=8, frameon=False)
    fig.tight_layout()
    fig.savefig(out, dpi=160, bbox_inches="tight")
    plt.close(fig)
    print("wrote", out)


def linear_bars(runs: dict[str, dict[str, float]], out: str) -> None:
    tags = sorted(runs)
    keys = [k for k in OP_ORDER if k.startswith("linear")]
    present = [k for k in keys if any(runs[t].get(k) for t in tags)]
    if not present or not tags:
        return
    width = 0.8 / len(present)
    fig, ax = plt.subplots(figsize=(8, 4))
    for i, key in enumerate(present):
        xs = [j + i * width for j in range(len(tags))]
        ax.bar(xs, [runs[t].get(key, 0.0) for t in tags], width=width, label=key)
    ax.set_xticks([j + 0.4 - width / 2 for j in range(len(tags))])
    ax.set_xticklabels(tags, rotation=15, ha="right")
    ax.set_ylabel("% of profiled time")
    ax.set_title("Linear layers: which projection dominates, per run")
    ax.legend(fontsize=8, frameon=False)
    fig.tight_layout()
    fig.savefig(out, dpi=160)
    plt.close(fig)
    print("wrote", out)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("csv", nargs="+")
    ap.add_argument("--out", default="figures")
    args = ap.parse_args()

    os.makedirs(args.out, exist_ok=True)
    stage_runs, op_runs = {}, {}

    for path in args.csv:
        tag = os.path.basename(path).replace("-stages.csv", "")
        with open(path) as f:
            rows = list(csv.DictReader(f))
        if not rows:
            continue
        for target, key in ((stage_runs, "stage"), (op_runs, "op_class")):
            agg = collections.Counter()
            for r in rows:
                agg[r[key]] += float(r["ms"])
            s = sum(agg.values()) or 1.0
            target[tag] = {k: 100.0 * v / s for k, v in agg.items()}

    stacked(stage_runs, STAGE_ORDER, "Where the time goes: engine stage share",
            "% of profiled time (CPU: host self time, GPU: device kernel time)",
            os.path.join(args.out, "fig1-stages.png"))
    stacked(op_runs, OP_ORDER, "What kind of work: op class share",
            "% of profiled time", os.path.join(args.out, "fig2-op-classes.png"))
    linear_bars(op_runs, os.path.join(args.out, "fig3-linear.png"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
