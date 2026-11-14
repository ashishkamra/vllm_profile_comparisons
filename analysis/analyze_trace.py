# SPDX-License-Identifier: Apache-2.0
"""Aggregate a torch/kineto trace into the stage taxonomy of docs/03-stage-taxonomy.md.

Works on CPU traces today; GPU traces are handled by correlating kernels back to the
CPU op that launched them (see docs/02-methodology.md#async-device-time).

Usage:
    analyze_trace.py --trace traces/cpu-int8-conc8/*.json.gz --out results/cpu-int8-conc8.csv
"""

from __future__ import annotations

import argparse
import bisect
import collections
import gzip
import json
import os
import sys

import stages

FRAME_CAT = "python_function"
KERNEL_CATS = {"kernel", "gpu_memcpy", "gpu_memset"}


def load(path: str) -> list[dict]:
    opener = gzip.open if path.endswith(".gz") else open
    with opener(path, "rt") as f:
        raw = json.load(f)
    events = raw["traceEvents"] if isinstance(raw, dict) else raw
    return events


def complete_events(events: list[dict]) -> list[dict]:
    return [e for e in events if e.get("ph") == "X" and e.get("dur")]


def self_times(events: list[dict]) -> dict[int, float]:
    """Self time per event id: duration minus the duration of direct children."""
    by_tid: dict[object, list[dict]] = collections.defaultdict(list)
    for e in events:
        by_tid[e.get("tid")].append(e)

    result: dict[int, float] = {}
    for evs in by_tid.values():
        evs.sort(key=lambda e: (e["ts"], -e["dur"]))
        stack: list[dict] = []
        for e in evs:
            e["_end"] = e["ts"] + e["dur"]
            e["_kids"] = 0.0
            while stack and stack[-1]["_end"] <= e["ts"]:
                top = stack.pop()
                if stack:
                    stack[-1]["_kids"] += top["dur"]
            stack.append(e)
        while stack:
            top = stack.pop()
            if stack:
                stack[-1]["_kids"] += top["dur"]
        for e in evs:
            result[id(e)] = max(e["dur"] - e["_kids"], 0.0)
    return result


class FrameIndex:
    """Innermost-enclosing python frame lookup, per thread."""

    def __init__(self, events: list[dict]):
        self._ts: dict[object, list[float]] = collections.defaultdict(list)
        self._frames: dict[object, list[tuple[float, float, str, str]]] = (
            collections.defaultdict(list)
        )
        for e in events:
            if e.get("cat") != FRAME_CAT:
                continue
            file, func = stages.parse_frame(e.get("name", ""))
            tid = e.get("tid")
            self._frames[tid].append((e["ts"], e["ts"] + e["dur"], file, func))
        for tid in self._frames:
            self._frames[tid].sort()
            self._ts[tid] = [f[0] for f in self._frames[tid]]

    def stage_at(self, tid, ts: float) -> str:
        frames = self._frames.get(tid)
        if not frames:
            return "other"
        i = bisect.bisect_right(self._ts[tid], ts) - 1
        while i >= 0:
            start, end, file, func = frames[i]
            if end >= ts:
                stage = stages.stage_for_frame(file, func)
                if stage != "other":
                    return stage
            i -= 1
        return "other"


def kernel_to_launcher(events: list[dict]) -> dict[int, dict]:
    """Map kernel event id -> the CPU runtime event that launched it (GPU traces)."""
    launchers: dict[str, dict] = {}
    for e in events:
        if e.get("cat") in ("cuda_runtime", "Runtime", "gpu_user_annotation"):
            corr = e.get("args", {}).get("correlation")
            if corr is not None:
                launchers[str(corr)] = e
    mapping: dict[int, dict] = {}
    for e in events:
        if e.get("cat") in KERNEL_CATS:
            corr = e.get("args", {}).get("correlation")
            if corr is not None and str(corr) in launchers:
                mapping[id(e)] = launchers[str(corr)]
    return mapping


def kind_of_trace(events: list[dict]) -> str:
    return "gpu" if any(e.get("cat") in KERNEL_CATS for e in events) else "cpu"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--trace", required=True, nargs="+")
    ap.add_argument("--out", default=None, help="CSV path")
    ap.add_argument("--vocab-size", type=int, default=151936)
    ap.add_argument("--top", type=int, default=20)
    args = ap.parse_args()

    out_rows: list[dict] = []
    for path in args.trace:
        events = complete_events(load(path))
        if not events:
            print(f"skip {path}: no complete events", file=sys.stderr)
            continue

        st = self_times(events)
        frames = FrameIndex(events)
        kind = kind_of_trace(events)
        launcher = kernel_to_launcher(events) if kind == "gpu" else {}

        metric = "device_ms" if kind == "gpu" else "self_ms"
        width = (max(e["_end"] for e in events) - min(e["ts"] for e in events)) / 1000.0

        # (stage, op_class) -> [ms, calls]
        agg: dict[tuple[str, str], list[float]] = collections.defaultdict(lambda: [0.0, 0])
        by_op: collections.Counter = collections.Counter()

        for e in events:
            if kind == "gpu":
                if e.get("cat") not in KERNEL_CATS:
                    continue  # host spans are reported separately below
                host = launcher.get(id(e), e)
                tid, ts = host.get("tid"), host.get("ts", e["ts"])
            else:
                if e.get("cat") == FRAME_CAT:
                    tid, ts = e.get("tid"), e["ts"]
                elif e.get("cat") in ("cpu_op", "user_annotation"):
                    tid, ts = e.get("tid"), e["ts"]
                else:
                    continue

            stage = frames.stage_at(tid, ts)
            op_class = stages.op_class_for(
                e.get("name", ""), e.get("args"), vocab_size=args.vocab_size
            )
            stage = stages.refine_stage(stage, op_class)
            ms = st.get(id(e), e["dur"]) / 1000.0
            agg[(stage, op_class)][0] += ms
            agg[(stage, op_class)][1] += 1
            by_op[e.get("name", "")] += ms

        total = sum(v[0] for v in agg.values()) or 1.0
        print(f"\n=== {os.path.basename(path)}  [{kind} trace, metric={metric}, "
              f"window={width:.1f} ms] ===")

        stage_totals = collections.Counter()
        for (stage, _), (ms, _n) in agg.items():
            stage_totals[stage] += ms

        print(f"\n-- stage ({metric}) --")
        for stage, ms in stage_totals.most_common():
            print(f"  {ms:9.2f} ms {100 * ms / total:5.1f}%  {stage}")
        other_pct = 100 * stage_totals["other"] / total
        if other_pct > 10:
            print(f"  !! 'other' is {other_pct:.1f}% — taxonomy needs updating")

        print(f"\n-- top {args.top} by {metric} --")
        for name, ms in by_op.most_common(args.top):
            print(f"  {ms:9.2f} ms {100 * ms / total:5.1f}%  {name[:100]}")

        print("\n-- (stage, op_class) --")
        for (stage, op_class), (ms, n) in sorted(
            agg.items(), key=lambda kv: -kv[1][0]
        )[: args.top]:
            print(f"  {ms:9.2f} ms {100 * ms / total:5.1f}%  {stage:22s} {op_class:16s} n={n}")

        for (stage, op_class), (ms, n) in agg.items():
            out_rows.append(
                {
                    "trace": os.path.basename(path),
                    "device": kind,
                    "metric": metric,
                    "stage": stage,
                    "op_class": op_class,
                    "ms": round(ms, 4),
                    "calls": n,
                    "pct": round(100 * ms / total, 3),
                }
            )

    if args.out and out_rows:
        fields = list(out_rows[0])
        with open(args.out, "w") as f:
            f.write(",".join(fields) + "\n")
            for r in out_rows:
                f.write(",".join(str(r[k]) for k in fields) + "\n")
        print(f"\nwrote {args.out} ({len(out_rows)} rows)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
