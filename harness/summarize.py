#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Turn GuideLLM's per-scenario benchmarks.json into one committed summary table.

    summarize.py --results results --out results/summary.csv

Only `benchmarks.json` is read. GuideLLM 0.8.1's own console summary can disagree
with its JSON for the same run -- on the CPU conc8 run it printed 15 completed
requests where the JSON records 8 successful + 8 incomplete -- so the console is
treated as a log, not as a source of numbers. Every row here is reproducible from
the JSON alone, and the sample counts are carried along so a mean is never quoted
without the n it came from.

Throughput is computed as a rate over the measured window
(`total tokens / duration`), not as a mean of per-request rates: the latter
weights a fast request as heavily as a slow one and does not sum to the server's
actual throughput.
"""

from __future__ import annotations

import argparse
import csv
import glob
import json
import os
import re
import sys


def preemption_delta(console: str) -> str:
    try:
        with open(console) as f:
            for line in f:
                m = re.match(r"preemptions_delta=(-?\d+)", line.strip())
                if m:
                    return m.group(1)
    except OSError:
        pass
    return ""


def mean_and_p95(metric: dict, field: str) -> tuple[float | None, float | None]:
    d = (metric.get(field) or {}).get("successful") or {}
    return d.get("mean"), (d.get("percentiles") or {}).get("p95")


def rows_for(path: str) -> list[dict]:
    with open(path) as f:
        report = json.load(f)
    tag_scenario = os.path.basename(os.path.dirname(path))
    console = os.path.join(os.path.dirname(path), "console.txt")
    delta = preemption_delta(console)

    rows = []
    for b in report.get("benchmarks", []):
        m = b.get("metrics") or {}
        totals = m.get("request_totals") or {}
        duration = float(b.get("duration") or 0.0)
        ok = int(totals.get("successful") or 0)
        out_tokens = ((m.get("output_token_count") or {}).get("successful") or {}).get(
            "total_sum"
        )
        ttft_mean, ttft_p95 = mean_and_p95(m, "time_to_first_token_ms")
        itl_mean, itl_p95 = mean_and_p95(m, "inter_token_latency_ms")
        strategy = (b.get("config") or {}).get("strategy") or {}

        rows.append(
            {
                "run": tag_scenario,
                "strategy": strategy.get("type_", "?"),
                "duration_s": round(duration, 1),
                "req_ok": ok,
                "req_incomplete": int(totals.get("incomplete") or 0),
                "req_errored": int(totals.get("errored") or 0),
                "req_per_s": round(ok / duration, 4) if duration else "",
                "out_tok_per_s": (
                    round(float(out_tokens) / duration, 2)
                    if duration and out_tokens is not None
                    else ""
                ),
                "ttft_ms_mean": round(ttft_mean, 1) if ttft_mean is not None else "",
                "ttft_ms_p95": round(ttft_p95, 1) if ttft_p95 is not None else "",
                "itl_ms_mean": round(itl_mean, 1) if itl_mean is not None else "",
                "itl_ms_p95": round(itl_p95, 1) if itl_p95 is not None else "",
                "preempt_delta": delta,
            }
        )
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--results", default="results")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    paths = sorted(glob.glob(os.path.join(args.results, "*", "benchmarks.json")))
    rows: list[dict] = []
    for p in paths:
        rows.extend(rows_for(p))
    if not rows:
        print(f"no benchmarks.json under {args.results}/", file=sys.stderr)
        return 1

    rows.sort(key=lambda r: (r["run"], r["strategy"]))
    fields = list(rows[0])
    if args.out:
        with open(args.out, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fields)
            w.writeheader()
            w.writerows(rows)
        print(f"wrote {args.out} ({len(rows)} rows)")
    else:
        w = csv.DictWriter(sys.stdout, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
