# SPDX-License-Identifier: Apache-2.0
"""Aggregate a torch/kineto trace into the stage taxonomy of docs/03-stage-taxonomy.md.

Two axes per event, as defined in that document:

  stage     which vLLM stage owns the work (from the enclosing Python frame)
  op_class  what kind of math it is (from the ATen op name + input shapes)

Only *work* is attributed: `cpu_op` / user annotations on CPU, device kernels on
GPU. Bare `python_function` self time is deliberately excluded — in a serving
trace it is dominated by asyncio/queue waits, which would drown out the model.
Its share is printed separately so the omission is visible, not hidden.

Usage:
    analyze_trace.py --trace traces/cpu-int8-conc8/*.json.gz --out results/cpu-int8-conc8.csv
"""

from __future__ import annotations

import argparse
import collections
import gzip
import json
import os
import sys

import stages

FRAME_CAT = "python_function"
# Host-side work on both legs: ATen ops and explicit annotations.
CPU_WORK_CATS = {"cpu_op", "user_annotation"}
KERNEL_CATS = {"kernel", "gpu_memcpy", "gpu_memset"}
# Anything that carries a `correlation` id and is not itself a kernel can be the
# launcher of one. Two spellings matter in practice: PyTorch records its own
# launches as `cuda_runtime` (cudaLaunchKernel), but cuBLAS -- which is where
# every GEMM on the GPU leg comes from -- launches through the driver API as
# `cuda_driver` (cuLaunchKernel/cuLaunchKernelEx). Without the latter, the
# largest single slice of GPU device time has no enclosing frame and lands in
# stage "other" (seen as 38.5% of a 0.6B decode trace).
LAUNCHER_CATS = (
    "cuda_runtime",
    "cuda_driver",
    "Runtime",
    "gpu_user_annotation",
)


def load(path: str) -> list[dict]:
    opener = gzip.open if path.endswith(".gz") else open
    with opener(path, "rt") as f:
        raw = json.load(f)
    return raw["traceEvents"] if isinstance(raw, dict) else raw


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


class StageIndex:
    """Maps (thread, timestamp) -> engine stage, via a per-thread sweep.

    Python frames are properly nested per thread, so one pass with a stack gives
    the innermost enclosing frame for every query. Querying ops in timestamp
    order makes the whole thing O((frames + ops) log n) instead of a backward
    scan per event.
    """

    def __init__(self, events: list[dict]):
        self._frames: dict[object, list[tuple[float, float, str, str]]] = (
            collections.defaultdict(list)
        )
        for e in events:
            if e.get("cat") != FRAME_CAT:
                continue
            file, func = stages.parse_frame(e.get("name", ""))
            self._frames[e.get("tid")].append(
                (e["ts"], e["ts"] + e["dur"], file, func)
            )
        for frames in self._frames.values():
            frames.sort(key=lambda f: f[0])
        self._cursor: dict[object, int] = collections.defaultdict(int)
        self._stack: dict[object, list[tuple[float, str]]] = collections.defaultdict(list)

    def stage_at(self, tid, ts: float) -> str:
        frames = self._frames.get(tid)
        if not frames:
            return "other"

        stack = self._stack[tid]
        i = self._cursor[tid]
        while i < len(frames) and frames[i][0] <= ts:
            start, end, file, func = frames[i]
            stack.append((end, stages.stage_for_frame(file, func)))
            i += 1
        self._cursor[tid] = i

        while stack and stack[-1][0] < ts:
            stack.pop()

        for _end, stage in reversed(stack):
            if stage != "other":
                return stage
        return "other"


class EnclosingOps:
    """Maps (thread, timestamp) -> the innermost `cpu_op` enclosing it.

    Device kernels carry no input shapes; `record_shapes` puts them on the
    *host* op that issued the launch (aten::linear and friends). A kernel runs
    on the CUDA stream thread, which has no host ops at all, so the lookup is
    done at its launcher's (tid, ts) -- the launcher is recorded inside the
    op that called into cuBLAS.

    Same single-pass sweep as StageIndex: callers query in timestamp order per
    thread, which makes this O((ops + queries) log n).
    """

    def __init__(self, events: list[dict]):
        self._ops: dict[object, list[tuple[float, float, dict]]] = (
            collections.defaultdict(list)
        )
        for e in events:
            if e.get("cat") != "cpu_op":
                continue
            self._ops[e.get("tid")].append((e["ts"], e["ts"] + e["dur"], e))
        for ops in self._ops.values():
            # Widest first, so a stack pop always removes the innermost op.
            ops.sort(key=lambda o: (o[0], -o[1]))
        self._cursor: dict[object, int] = collections.defaultdict(int)
        self._stack: dict[object, list[tuple[float, dict]]] = collections.defaultdict(list)

    def op_at(self, tid, ts: float) -> dict | None:
        ops = self._ops.get(tid)
        if not ops:
            return None

        stack = self._stack[tid]
        i = self._cursor[tid]
        while i < len(ops) and ops[i][0] <= ts:
            _start, end, op = ops[i]
            stack.append((end, op))
            i += 1
        self._cursor[tid] = i

        while stack and stack[-1][0] < ts:
            stack.pop()

        return stack[-1][1] if stack else None


def gpu_op_class(kernel: dict, host_op: dict | None, *, vocab_size: int) -> str:
    """Op class for a device kernel, refined through its enclosing host op.

    Kernels name themselves ("cutlass_80_wmma...gemm"), which says it is a GEMM
    but not *which* projection: on the GPU leg every GEMM therefore collapses
    into one coarse `linear` bucket, while the CPU leg splits into
    qkv/o_proj/gate_up/down because shapes are available there.

    The split needs both the shape and the convention it was recorded in, so the
    enclosing op is classified from its own name and args rather than by
    borrowing its dims -- `aten::linear` orders Input Dims as [input, weight],
    vLLM's custom kernels as [out, input, ...]. Only a coarse `linear` result is
    refined, and only by another linear class; anything else leaves the kernel's
    own verdict alone.
    """
    op_class = stages.op_class_for(
        kernel.get("name", ""), kernel.get("args"), vocab_size=vocab_size
    )
    if op_class != "linear" or host_op is None:
        return op_class
    refined = stages.op_class_for(
        host_op.get("name", ""), host_op.get("args"), vocab_size=vocab_size
    )
    return refined if refined.startswith("linear") else op_class


def kernel_to_launcher(events: list[dict]) -> dict[int, dict]:
    """Map kernel event id -> the CPU runtime event that launched it (GPU traces)."""
    launchers: dict[str, dict] = {}
    for e in events:
        if e.get("cat") in LAUNCHER_CATS:
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


def work_events(events: list[dict], kind: str) -> list[dict]:
    """The events whose self time is attributed, in timestamp order per thread."""
    if kind == "gpu":
        picked = [e for e in events if e.get("cat") in KERNEL_CATS]
    else:
        picked = [e for e in events if e.get("cat") in CPU_WORK_CATS]
    picked.sort(key=lambda e: (e.get("tid"), e["ts"]))
    return picked


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

        kind = kind_of_trace(events)
        work = work_events(events, kind)
        if not work:
            print(
                f"skip {path}: no {'kernels' if kind == 'gpu' else 'ATen ops'} — "
                "this looks like a frontend-only trace; profile with "
                "VLLM_ENABLE_V1_MULTIPROCESSING=0",
                file=sys.stderr,
            )
            continue

        st = self_times(events)
        index = StageIndex(events)
        launcher = kernel_to_launcher(events) if kind == "gpu" else {}
        enclosing = EnclosingOps(events) if kind == "gpu" else None
        metric = "device_ms" if kind == "gpu" else "self_ms"

        # (stage, op_class) -> [ms, calls]
        agg: dict[tuple[str, str], list[float]] = collections.defaultdict(lambda: [0.0, 0])
        by_op: collections.Counter = collections.Counter()

        for e in work:
            if kind == "gpu":
                host = launcher.get(id(e), e)
                tid, ts = host.get("tid"), host.get("ts", e["ts"])
            else:
                tid, ts = e.get("tid"), e["ts"]

            stage = index.stage_at(tid, ts)
            if kind == "gpu":
                op_class = gpu_op_class(
                    e, enclosing.op_at(tid, ts), vocab_size=args.vocab_size
                )
            else:
                op_class = stages.op_class_for(
                    e.get("name", ""), e.get("args"), vocab_size=args.vocab_size
                )
            stage = stages.refine_stage(stage, op_class)
            ms = st.get(id(e), e["dur"]) / 1000.0
            agg[(stage, op_class)][0] += ms
            agg[(stage, op_class)][1] += 1
            by_op[e.get("name", "")] += ms

        total = sum(v[0] for v in agg.values()) or 1.0

        # Idle/wait context: python self time is not attributed, but its size
        # explains how much of the trace window is not "work" on this thread.
        python_self = sum(
            st.get(id(e), 0.0) for e in events if e.get("cat") == FRAME_CAT
        ) / 1000.0

        print(f"\n=== {os.path.basename(path)}  [{kind} trace, metric={metric}] ===")
        print(f"  work events: {len(work)}   attributed: {total:.1f} ms   "
              f"python self time (not attributed): {python_self:.1f} ms")

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
            print(f"  {ms:9.2f} ms {100 * ms / total:5.1f}%  "
                  f"{stage:22s} {op_class:16s} n={n}")

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
