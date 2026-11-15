#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Print the call tree of one thread of a torch/kineto trace over a time window.

An exploration tool: the quickest way to see what a vLLM process was doing at a
given moment, with frames that are siblings and share a name collapsed into one
line (`x37`) so a 28-layer forward reads as one block instead of 28.

    trace_tree.py TRACE --list-threads
    trace_tree.py TRACE --tid 12345 --start 1794745620.10 --dur-ms 200 --depth 8
    trace_tree.py TRACE --tid 12345 --annotation 3 --depth 10   # the 4th step

Times are Unix seconds: every vLLM trace is stamped with baseTimeNanoseconds,
and baseTimeNanoseconds/1e3 + ts is the wall clock in microseconds, which is
what lets traces from different processes be read on one timeline.
"""

from __future__ import annotations

import argparse
import gzip
import json
import re
import sys
from collections import defaultdict

WORK_CATS = {"python_function", "cpu_op", "user_annotation", "gpu_user_annotation",
             "cuda_runtime", "cuda_driver", "kernel", "gpu_memcpy", "gpu_memset"}


def load(path: str) -> tuple[list[dict], float]:
    opener = gzip.open if path.endswith(".gz") else open
    with opener(path, "rt") as f:
        raw = json.load(f)
    base_us = raw.get("baseTimeNanoseconds", 0) / 1e3 if isinstance(raw, dict) else 0.0
    events = raw["traceEvents"] if isinstance(raw, dict) else raw
    return events, base_us


def short(name: str) -> str:
    """Shorten 'path/vllm/v1/engine/core.py(667): step' -> 'v1/engine/core.py:667 step'."""
    m = re.match(r"(.*?)\((\d+)\): (.*)", name)
    if not m:
        return name[:140]
    path, line, func = m.groups()
    for marker in ("/vllm/", "/site-packages/", "/lib/python3"):
        if marker in path:
            path = path.split(marker, 1)[1]
            break
    return f"{func}  [{path}:{line}]"


def build_tree(events: list[dict], lo: float, hi: float) -> list[dict]:
    """Nest complete events by time containment (one thread's events)."""
    evs = sorted(
        (e for e in events if e["ts"] < hi and e["ts"] + e.get("dur", 0) > lo),
        key=lambda e: (e["ts"], -e.get("dur", 0)),
    )
    roots: list[dict] = []
    stack: list[dict] = []
    for e in evs:
        node = {"e": e, "kids": []}
        end = e["ts"] + e.get("dur", 0)
        while stack and stack[-1]["e"]["ts"] + stack[-1]["e"].get("dur", 0) < end - 1e-3:
            stack.pop()
        (stack[-1]["kids"] if stack else roots).append(node)
        stack.append(node)
    return roots


def render(nodes: list[dict], depth: int, max_depth: int, min_ms: float, out: list[str]) -> None:
    # collapse runs of siblings with the same name
    groups: list[list[dict]] = []
    for n in nodes:
        if groups and short(groups[-1][0]["e"]["name"]) == short(n["e"]["name"]):
            groups[-1].append(n)
        else:
            groups.append([n])
    for g in groups:
        total = sum(n["e"].get("dur", 0) for n in g) / 1e3
        if total < min_ms:
            continue
        e = g[0]["e"]
        tag = e.get("cat", "")[:4]
        rep = f" x{len(g)}" if len(g) > 1 else ""
        out.append(f"{'  ' * depth}{total:9.3f} ms  {tag:4s} {short(e['name'])}{rep}")
        if depth + 1 < max_depth:
            kids = [k for n in g for k in n["kids"]] if len(g) > 1 else g[0]["kids"]
            if len(g) > 1:
                # merged children of a collapsed group: show the first member's subtree
                kids = g[0]["kids"]
                out.append(f"{'  ' * (depth + 1)}(subtree of the first of {len(g)})")
            render(kids, depth + 1, max_depth, min_ms, out)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("trace")
    ap.add_argument("--list-threads", action="store_true")
    ap.add_argument("--tid", type=int)
    ap.add_argument("--start", type=float, help="Unix seconds")
    ap.add_argument("--dur-ms", type=float, default=100.0)
    ap.add_argument("--annotation", type=int, help="index of execute_* step annotation to use as window")
    ap.add_argument("--depth", type=int, default=6)
    ap.add_argument("--min-ms", type=float, default=0.05)
    ap.add_argument("--root", help="regex: start the tree at frames whose name matches")
    args = ap.parse_args()

    events, base = load(args.trace)
    events = [e for e in events if e.get("ph") == "X" and "ts" in e]
    for e in events:
        e["ts"] = e["ts"] + base  # absolute microseconds

    if args.list_threads:
        names = {}
        raw, _ = load(args.trace)
        for e in raw:
            if e.get("ph") == "M" and e.get("name") == "thread_name":
                names[e["tid"]] = e["args"]["name"]
        per = defaultdict(lambda: [0, 0.0, float("inf"), 0.0])
        for e in events:
            p = per[(e.get("pid"), e.get("tid"))]
            p[0] += 1
            p[2] = min(p[2], e["ts"])
            p[3] = max(p[3], e["ts"] + e.get("dur", 0))
        for (pid, tid), (n, _, a, b) in sorted(per.items(), key=lambda kv: -kv[1][0]):
            print(f"pid {pid} tid {tid}  {names.get(tid, '')}  events {n}  span {a / 1e6:.3f}-{b / 1e6:.3f}")
        return 0

    if args.annotation is not None:
        steps = sorted((e for e in events if e.get("cat") == "user_annotation"
                        and e["name"].startswith("execute_")), key=lambda e: e["ts"])
        s = steps[args.annotation]
        lo, hi = s["ts"], s["ts"] + s["dur"]
        tid = args.tid if args.tid is not None else s["tid"]
        print(f"window = step {args.annotation}: {s['name']}  {s['dur'] / 1e3:.2f} ms")
    else:
        if args.start is None or args.tid is None:
            sys.exit("need --tid and --start (or --annotation)")
        lo = args.start * 1e6
        hi = lo + args.dur_ms * 1e3
        tid = args.tid
    mine = [e for e in events if e.get("tid") == tid and e.get("cat") in WORK_CATS]
    out: list[str] = []
    roots = build_tree(mine, lo, hi)
    if args.root:
        rx = re.compile(args.root)
        found: list[dict] = []

        def walk(nodes: list[dict]) -> None:
            for n in nodes:
                if rx.search(n["e"]["name"]):
                    found.append(n)
                else:
                    walk(n["kids"])

        walk(roots)
        roots = found
    render(roots, 0, args.depth, args.min_ms, out)
    print("\n".join(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
