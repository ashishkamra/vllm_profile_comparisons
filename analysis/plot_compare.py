# SPDX-License-Identifier: Apache-2.0
"""Render comparison figures from analyze_trace.py CSVs.

    plot_compare.py results/*-stages.csv --out figures

Three figures, because "where did the time go" has three honest answers:

  fig1-stages.png      absolute attributed time per engine stage, per run
  fig2-op-classes.png  share of work by op class, per run
  fig3-linear.png      absolute time per projection, per run

Shares and absolutes are deliberately kept apart. A share bar answers "what
dominates this run"; it cannot answer "did this code get cheaper", because the
denominator moves too (INT8 cut total attributed work by 1.5x here). Every run
in a leg executes the same number of GEMMs for the same 20 iterations, so
absolute milliseconds are comparable across variants and shares are not.
"""

from __future__ import annotations

import argparse
import collections
import csv
import os
import re

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.ticker as ticker

STAGE_ORDER = [
    "frontend.tokenize",
    "frontend.other",
    "engine.schedule",
    "engine.kv_cache",
    "engine.postprocess",
    "runner.prepare_inputs",
    "runner.execute_model",
    "runner.glue",
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
    "quant",
    "sampler",
    "python",
]

# Reference first, then the deployment artifacts: the baseline is what the eye
# should compare against.
VARIANT_RANK = {"ref": 0, "int8": 1, "fp8": 2}


def run_order(tag: str) -> tuple:
    """Sort runs by scenario, then device, then reference-before-quantized."""
    parts = tag.split("-")
    device = parts[0]
    variant = parts[1] if len(parts) > 1 else ""
    scenario = "-".join(parts[2:])
    return (scenario, device, VARIANT_RANK.get(variant, 9), tag)


def _annotate(ax, xs, ys, vals) -> None:
    for x, y, v in zip(xs, ys, vals):
        ax.text(x, y, f" {v:.1f} ms" if v >= 1 else f" {v:.2f} ms",
                va="center", fontsize=7)


def absolute_bars(runs: dict[str, dict[str, float]], order: list[str], title: str,
                  xlabel: str, out: str, ylabel_width: int = 0) -> None:
    """Grouped horizontal bars of absolute ms, log x, values annotated.

    Log x because the stages span four orders of magnitude (a sampler call next
    to a forward pass). Values are annotated so nothing has to be read off a log
    axis.
    """
    tags = sorted(runs, key=run_order)
    if not tags:
        return
    keys = [k for k in order if any(k in runs[t] for t in tags)]
    keys += sorted({k for t in tags for k in runs[t]} - set(keys))
    # Stages that are present but tiny still deserve a row; drop only all-zero.
    keys = [k for k in keys if any(runs[t].get(k, 0.0) > 0 for t in tags)]
    if not keys:
        return

    height = 0.8 / len(tags)
    cmap = plt.get_cmap("tab10")
    fig, ax = plt.subplots(figsize=(9.5, 0.55 * len(keys) + 2.0))
    positive = [v for t in tags for v in runs[t].values() if v > 0]
    floor = min(positive) / 4 if positive else 0.01

    for i, tag in enumerate(tags):
        ys = [j - 0.4 + height / 2 + i * height for j in range(len(keys))]
        vals = [runs[tag].get(k, 0.0) for k in keys]
        ax.barh(ys, [max(v, floor) for v in vals], height=height,
                label=tag, color=cmap(i % 10))
        _annotate(ax, [max(v, floor) for v in vals], ys, vals)

    ax.set_yticks(range(len(keys)))
    ax.set_yticklabels(keys)
    ax.invert_yaxis()
    ax.set_xscale("log")
    ax.set_xlabel(xlabel)
    ax.set_title(title)
    # leave room for the value labels at the right edge
    ax.set_xlim(left=floor, right=max(positive) * 2 if positive else 1)
    ax.xaxis.set_major_locator(ticker.LogLocator(base=10, subs=(1, 2, 5)))
    ax.xaxis.set_minor_locator(ticker.NullLocator())
    ax.xaxis.set_major_formatter(ticker.FuncFormatter(lambda v, _p: f"{v:g}"))
    ax.grid(axis="x", which="major", alpha=0.3)
    ax.tick_params(axis="x", labelsize=8)
    ax.legend(fontsize=8, frameon=False, loc="lower right")
    fig.tight_layout()
    fig.savefig(out, dpi=160, bbox_inches="tight")
    plt.close(fig)
    print("wrote", out)


def stacked(runs: dict[str, dict[str, float]], order: list[str], title: str,
            xlabel: str, out: str) -> None:
    tags = sorted(runs, key=run_order)
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


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("csv", nargs="+")
    ap.add_argument("--out", default="figures")
    args = ap.parse_args()

    os.makedirs(args.out, exist_ok=True)
    stage_ms: dict[str, dict[str, float]] = {}
    op_ms: dict[str, dict[str, float]] = {}

    for path in args.csv:
        tag = re.sub(r"-stages\.csv$", "", os.path.basename(path))
        with open(path) as f:
            rows = list(csv.DictReader(f))
        if not rows:
            continue
        for target, key in ((stage_ms, "stage"), (op_ms, "op_class")):
            agg = collections.Counter()
            for r in rows:
                agg[r[key]] += float(r["ms"])
            target[tag] = dict(agg)

    def shares(ms: dict[str, dict[str, float]]) -> dict[str, dict[str, float]]:
        out = {}
        for tag, agg in ms.items():
            total = sum(agg.values()) or 1.0
            out[tag] = {k: 100.0 * v / total for k, v in agg.items()}
        return out

    absolute_bars(
        stage_ms, STAGE_ORDER,
        "Where the time goes: attributed time per engine stage",
        "attributed time (ms, log scale; CPU host self time / GPU kernel time)",
        os.path.join(args.out, "fig1-stages.png"))
    stacked(shares(op_ms), OP_ORDER, "What kind of work: op class share",
            "% of attributed time", os.path.join(args.out, "fig2-op-classes.png"))
    absolute_bars(
        {t: {k: v for k, v in agg.items() if k.startswith("linear")}
         for t, agg in op_ms.items()},
        OP_ORDER, "Linear layers: cost of each projection",
        "attributed time (ms, log scale)",
        os.path.join(args.out, "fig3-linear.png"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
