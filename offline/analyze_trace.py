# SPDX-License-Identifier: Apache-2.0
"""Summarise a torch/kineto CPU trace: self-time by op, by category, by Python frame.

Usage: analyze_trace.py <trace.json[.gz]> [--top 30]
"""

import argparse
import collections
import gzip
import json
import os
import re

p = argparse.ArgumentParser()
p.add_argument("trace")
p.add_argument("--top", type=int, default=25)
args = p.parse_args()


def load(path: str):
    if path.endswith(".gz"):
        with gzip.open(path, "rt") as f:
            return json.load(f)
    with open(path) as f:
        return json.load(f)


raw = load(args.trace)
events = raw["traceEvents"] if isinstance(raw, dict) else raw
xs = [e for e in events if e.get("ph") == "X" and e.get("dur")]
if not xs:
    raise SystemExit("no complete ('X') events in trace")

print(f"trace: {os.path.basename(args.trace)}")
print(f"events: {len(xs)}   threads: {len({e.get('tid') for e in xs})}")

# ---- per-thread nesting -> self time -------------------------------------
by_tid = collections.defaultdict(list)
for e in xs:
    by_tid[e.get("tid")].append(e)

self_us = {}
for tid, evs in by_tid.items():
    evs.sort(key=lambda e: (e["ts"], -e["dur"]))
    stack = []
    for e in evs:
        e["_kids"] = 0.0
        end = e["ts"] + e["dur"]
        while stack and stack[-1]["_end"] <= e["ts"]:
            top = stack.pop()
            if stack:
                stack[-1]["_kids"] += top["dur"]
        e["_end"] = end
        stack.append(e)
    while stack:
        top = stack.pop()
        if stack:
            stack[-1]["_kids"] += top["dur"]
    for e in evs:
        self_us[id(e)] = max(e["dur"] - e["_kids"], 0.0)

# profiled wall clock: widest span on the busiest thread
busiest = max(by_tid.items(), key=lambda kv: len(kv[1]))
span = (min(e["ts"] for e in busiest[1]), max(e["_end"] for e in busiest[1]))
wall_us = span[1] - span[0]
print(f"profiled window: {wall_us / 1000:.1f} ms (busiest thread tid={busiest[0]})")

# ---- category of an event ------------------------------------------------
def category(e) -> str:
    cat = e.get("cat", "")
    name = e.get("name", "")
    if cat == "python_function":
        return "python"
    if name.startswith("aten::") or name.startswith("aten."):
        return "torch-op"
    if cat in ("cpu_op", "user_annotation", "Runtime", "gpu_user_annotation"):
        return "torch-op" if cat == "cpu_op" else cat
    return cat or "other"


tot = collections.Counter()
for e in xs:
    tot[category(e)] += self_us[id(e)]

print("\n== self time by category ==")
for k, v in tot.most_common():
    if v <= 0:
        continue
    print(f"  {k:20s} {v / 1000:9.1f} ms  {100 * v / wall_us:5.1f}%")

# ---- top ops by self time -----------------------------------------------
ops = collections.Counter()
for e in xs:
    if category(e) in ("torch-op", "user_annotation", "python_function"):
        ops[e["name"]] += self_us[id(e)]

print(f"\n== top {args.top} by self time ==")
cum = 0.0
for name, v in ops.most_common(args.top):
    cum += v
    print(f"  {v / 1000:9.1f} ms {100 * v / wall_us:5.1f}%  {name[:110]}")

# ---- attribute time to vLLM source files (python frames + stacks) --------
FILE_RE = re.compile(r"([\w./-]+\.py)\((\d+)\): ?\(?([\w.<>]+)?")


def vllm_file(e):
    for blob in (e.get("name", ""), json.dumps(e.get("args", {}))):
        m = FILE_RE.search(blob)
        if m and "vllm/" in m.group(1):
            return m.group(1).split("vllm/")[-1], m.group(3)
    return None


files = collections.Counter()
pairs = collections.Counter()
for e in xs:
    f = vllm_file(e)
    if f:
        files[f[0]] += self_us[id(e)]
        pairs[(f[0], f[1])] += self_us[id(e)]

if files:
    print("\n== self time attributed to vLLM python frames ==")
    for name, v in files.most_common(15):
        print(f"  {v / 1000:9.1f} ms {100 * v / wall_us:5.1f}%  vllm/{name}")
    print("\n  (function level)")
    for (fn, func), v in pairs.most_common(15):
        print(f"  {v / 1000:9.1f} ms  {fn}:{func}")
