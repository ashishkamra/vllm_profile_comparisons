#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Merge two analyze_trace.py CSVs into one CPU-vs-GPU table.

    compare_runs.py --cpu results/cpu-ref-profile-stages.csv \
                    --gpu results/gpu-ref-profile-stages.csv \
                    --out results/profile-compare.csv

Rows are op classes, because that is the axis on which the two legs are
comparable: the *stage* split moves with the device (sampling and scheduling are
host work and do not appear as GPU device time at all), while an op class means
the same math on both. The stage is carried along as context.

Two cautions, printed with the table so they travel with the numbers:

  * The metric is not the same physical quantity. CPU is host self time (which on
    a CPU *is* the device time); GPU is summed kernel time. They answer "what
    does this op cost on this device", not "which machine is faster at serving".
  * The dtypes differ by design (fp32 vs bf16), so a ratio is not a pure hardware
    ratio; the GPU reads and multiplies half the bytes.

Share columns are the honest ones for "where did the time go"; absolute columns
are what show how far apart the two are for the same projection.
"""

from __future__ import annotations

import argparse
import collections
import csv

# Same order as plot_compare.py: projections first, then the rest.
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


def totals(path: str) -> tuple[dict[str, float], dict[str, str], float]:
    with open(path) as f:
        rows = list(csv.DictReader(f))
    ms: dict[str, float] = collections.Counter()
    by_stage: dict[str, dict[str, float]] = collections.defaultdict(
        lambda: collections.Counter()
    )
    for r in rows:
        ms[r["op_class"]] += float(r["ms"])
        by_stage[r["op_class"]][r["stage"]] += float(r["ms"])
    # One stage per class: where most of its time was spent, not the first row
    # seen -- elementwise work, for instance, spans five different stages.
    stage = {k: max(v, key=v.get) for k, v in by_stage.items()}
    return dict(ms), stage, sum(ms.values()) or 1.0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cpu", required=True)
    ap.add_argument("--gpu", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    cpu, cpu_stage, cpu_total = totals(args.cpu)
    gpu, gpu_stage, gpu_total = totals(args.gpu)

    keys = [k for k in OP_ORDER if k in cpu or k in gpu]
    keys += sorted((set(cpu) | set(gpu)) - set(keys))

    out_rows = []
    print(f"{'op_class':20s} {'stage':16s} {'cpu ms':>9s} {'cpu %':>6s} "
          f"{'gpu ms':>8s} {'gpu %':>6s} {'cpu/gpu':>8s}")
    for k in keys:
        c, g = cpu.get(k, 0.0), gpu.get(k, 0.0)
        ratio = f"{c / g:8.1f}x" if c and g else f"{'-':>9s}"
        stage = cpu_stage.get(k) or gpu_stage.get(k, "")
        print(f"{k:20s} {stage:16s} {c:9.2f} {100 * c / cpu_total:6.1f} "
              f"{g:8.2f} {100 * g / gpu_total:6.1f} {ratio:>8s}")
        out_rows.append({
            "op_class": k, "stage": stage,
            "cpu_ms": round(c, 4), "cpu_pct": round(100 * c / cpu_total, 3),
            "gpu_ms": round(g, 4), "gpu_pct": round(100 * g / gpu_total, 3),
            "cpu_over_gpu": round(c / g, 3) if c and g else "",
        })

    print(f"{'TOTAL':20s} {'':16s} {cpu_total:9.2f} {100.0:6.1f} "
          f"{gpu_total:8.2f} {100.0:6.1f} {cpu_total / gpu_total:8.1f}x")
    print("\nCPU metric is host self time, GPU metric is summed kernel time;")
    print("dtypes differ by design (cpu fp32, gpu bf16), so ratios are not")
    print("pure hardware ratios.")

    with open(args.out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(out_rows[0]))
        w.writeheader()
        w.writerows(out_rows)
    print(f"\nwrote {args.out} ({len(out_rows)} rows)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
