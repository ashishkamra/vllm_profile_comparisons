#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Explain one engine step from a profiler trace.

    step_report.py --trace traces/cpu-int8-conc8/rank0*.json.gz
    step_report.py --trace traces/cpu-int8-conc8/rank0*.json.gz \
                   --fig figures/fig4-step-anatomy.png

analyze_trace.py answers "where did the time go overall". This answers the next
question down: *what is a step made of, and what did the scheduler decide in it*.

The step boundary comes from the worker's own annotation, written by
`Worker.annotate_profile` (v1/worker/gpu_worker.py) around
`model_runner.execute_model`:

    execute_context_<n_ctx_reqs>(<n_ctx_tokens>)_generation_<n_gen_reqs>(<n_gen_tokens>)

so the *batch composition is in the trace*, from the scheduler, not inferred by
us. One step executes `n_ctx_tokens + n_gen_tokens` tokens through every weight,
which is why a profiled prefill step is 5x a decode step at the same batch.

Sampling is NOT inside that annotation: `execute_model` stashes logits and
returns, and the engine core calls `sample_tokens` as a separate RPC
(v1/engine/core.py step()). Sampling therefore shows up in the inter-step gap,
next to schedule() and update_from_output().

Inside a layer, each op is attributed to the innermost `nn.Module` frame that
contains it, not to its ATen name. That matters on CPU: RMSNorm is decomposed
into aten::pow/mul/mean/div_ (no `norm` op exists) and the INT8 activation
quantize lives inside the linear that consumes it, so name-based classification
would scatter both. One-dimensional `nn.Module` frames (Qwen3Attention, Qwen2MLP)
carry the scope needed to tell o_proj's RowParallelLinear from MLP down's.

Layer 0 is reported separately rather than dropped: on the first decode step it
absorbs a one-time per-shape setup (on CPU the oneDNN primitive for the new M,
on GPU the first launch of kernels for these shapes), so it is several times the
other layers there and close to them by the end of the window. The means use
layers 1..N-1.

Works on CPU and GPU traces. On CPU the metric is host self time; on GPU the
per-op numbers are device (kernel) time attributed through the launching op, so
"forward" is device time while the step columns are host spans.
"""

from __future__ import annotations

import argparse
import collections
import glob
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import analyze_trace as at
import stages

STEP_RE = re.compile(r"execute_context_(\d+)\((\d+)\)_generation_(\d+)\((\d+)\)")

# Frame names end in "path.py(line): func", so match on the function suffix.
PHASES = {
    "state": ": _update_states",
    "prepare": ": _prepare_inputs",
    "forward": ": _model_forward",
    "logits": ": compute_logits",
}
GAP_PHASES = {
    "sample": ": sample_tokens",
    "schedule": ": schedule",
    "postproc": ": update_from_output",
}

# Scope frames: everything nested inside them is attention or MLP respectively.
SCOPE_RX = re.compile(r"nn\.Module: (Qwen3Attention|Qwen2MLP)_\d+$")
SCOPE_OF = {"Qwen3Attention": "attn", "Qwen2MLP": "mlp"}

# Leaf modules. RowParallelLinear is resolved against the enclosing scope.
MODULE_LABELS = (
    (re.compile(r"nn\.Module: RMSNorm_\d+$"), "norm"),
    (re.compile(r"nn\.Module: QKVParallelLinear_\d+$"), "attn.qkv"),
    (re.compile(r"nn\.Module: RotaryEmbedding_\d+$"), "rope"),
    (re.compile(r"nn\.Module: Attention_\d+$"), "attn.core"),
    (re.compile(r"nn\.Module: MergedColumnParallelLinear_\d+$"), "mlp.gate_up"),
    (re.compile(r"nn\.Module: ColumnParallelLinear_\d+$"), "mlp.gate_up"),
    (re.compile(r"nn\.Module: SiluAndMul_\d+$"), "mlp.act"),
    (re.compile(r"nn\.Module: ParallelLMHead_\d+$"), "lm_head"),
)
ROW_RX = re.compile(r"nn\.Module: RowParallelLinear_\d+$")

COMPONENT_ORDER = (
    "attn.qkv",
    "rope",
    "attn.core",
    "attn.o_proj",
    "norm",
    "mlp.gate_up",
    "mlp.act",
    "mlp.down",
    "quant",
    "elementwise",
    "other",
    "unattributed",
)

# Fallback grouping when an op sits outside every labelled module frame.
GROUPS = {
    "attn.qkv": ("linear.qkv",),
    "attn.core": ("attention.paged", "attention.kv_write"),
    "attn.o_proj": ("linear.o_proj",),
    "rope": ("rope",),
    "mlp.gate_up": ("linear.gate_up",),
    "mlp.act": ("activation",),
    "mlp.down": ("linear.down",),
    "lm_head": ("linear.lm_head",),
    "norm": ("norm",),
}


def group_of(op_class: str) -> str:
    for group, members in GROUPS.items():
        if op_class in members:
            return group
    return op_class


def phase_ms(events: list[dict], suffixes: dict[str, str], lo: float, hi: float) -> dict:
    """Duration of the (outermost) frame ending in each suffix, inside [lo, hi]."""
    out = {k: 0.0 for k in suffixes}
    for e in events:
        if e.get("cat") != "python_function":
            continue
        if not (lo <= e["ts"] and e["ts"] + e["dur"] <= hi):
            continue
        for key, suffix in suffixes.items():
            if e["name"].endswith(suffix):
                out[key] = max(out[key], e["dur"] / 1000.0)
    return out


def layer_frames(events: list[dict], lo: float, hi: float, pattern: re.Pattern) -> list[dict]:
    """One frame per layer index, the outermost, inside [lo, hi]."""
    best: dict[int, dict] = {}
    for e in events:
        if e.get("cat") != "python_function":
            continue
        if not (lo <= e["ts"] and e["ts"] + e["dur"] <= hi):
            continue
        m = pattern.search(e["name"])
        if not m:
            continue
        idx = int(m.group(1))
        if idx not in best or e["dur"] > best[idx]["dur"]:
            best[idx] = e
    return [best[i] for i in sorted(best)]


def work_items(
    events: list[dict], kind: str, self_ms: dict[int, float], vocab_size: int = 151936
) -> list[tuple]:
    """(host event, ms, name, args, op_class) for attributed work.

    The host event is the one to test for containment: the op itself on CPU, the
    *launching* op on GPU, because a kernel's timestamp is on the device clock
    and does not fall inside the Python frame that requested it.

    The class is resolved here, not at attribution time, because on GPU the
    kernel name alone is coarse ("a gemm") and the enclosing op is what says
    which projection it is. Kernels launched through vLLM's compile wrapper skip
    nn.Module.__call__, so they carry no module frame at all; without this they
    fall back to a bare "linear" component (35.8% of prefill in one trace).
    """
    launcher = at.kernel_to_launcher(events) if kind == "gpu" else {}
    enclosing = at.EnclosingOps(events) if kind == "gpu" else None
    items = []
    for e in at.work_events(events, kind):
        host = launcher.get(id(e), e)
        if kind == "gpu":
            op_class = at.gpu_op_class(
                e,
                enclosing.op_at(host.get("tid"), host.get("ts", e["ts"])),
                vocab_size=vocab_size,
            )
        else:
            op_class = stages.op_class_for(
                e.get("name", ""), e.get("args"), vocab_size=vocab_size
            )
        items.append(
            (host, self_ms.get(id(e), e["dur"]) / 1000.0, e.get("name", ""),
             e.get("args"), op_class)
        )
    return items


def step_window(events: list[dict], annotation: dict) -> tuple[list[dict], list]:
    """Events fully inside one step, plus their containment parents."""
    lo, hi = annotation["ts"], annotation["ts"] + annotation["dur"]
    win = [e for e in events if lo <= e["ts"] and e["ts"] + e["dur"] <= hi]
    win.sort(key=lambda e: (e["ts"], -e.get("dur", 0)))

    parent = [None] * len(win)
    stack: list[int] = []
    for i, e in enumerate(win):
        end = e["ts"] + e["dur"]
        while stack:
            p = win[stack[-1]]
            if p["ts"] <= e["ts"] and end <= p["ts"] + p["dur"]:
                break
            stack.pop()
        parent[i] = stack[-1] if stack else None
        stack.append(i)
    return win, parent


def label_resolver(win: list[dict], parent: list):
    """event index -> (component label, scope), inherited from the nearest frame."""

    def self_label(e: dict, label: str | None, scope: str | None) -> tuple:
        if e.get("cat") != "python_function":
            return label, scope
        name = e["name"]
        m = SCOPE_RX.match(name)
        if m:
            return label, SCOPE_OF[m.group(1)]
        for rx, lab in MODULE_LABELS:
            if rx.match(name):
                return lab, scope
        if ROW_RX.match(name):
            if scope == "attn":
                return "attn.o_proj", scope
            if scope == "mlp":
                return "mlp.down", scope
            return label, scope
        return label, scope

    memo: dict[int, tuple] = {}

    def resolve(i: int) -> tuple:
        path = []
        j = i
        while j is not None and j not in memo:
            path.append(j)
            j = parent[j]
        label, scope = memo[j] if j is not None else (None, None)
        for k in reversed(path):
            label, scope = self_label(win[k], label, scope)
            memo[k] = (label, scope)
        return memo[i]

    return resolve


def attribute_layers(
    layers: list[dict],
    win: list[dict],
    resolve,
    items: list[tuple],
) -> list[tuple[float, collections.Counter]]:
    """[(layer ms, {component: ms})] for the given layer frames."""
    idx_of = {id(e): i for i, e in enumerate(win)}
    out = []
    for layer in layers:
        lo, hi = layer["ts"], layer["ts"] + layer["dur"]
        agg: collections.Counter = collections.Counter()
        for host, ms, name, args, op_class in items:
            if not (lo <= host["ts"] <= hi):
                continue
            comp = None
            if op_class == "quant":
                comp = "quant"
            else:
                i = idx_of.get(id(host))
                if i is not None:
                    comp, _ = resolve(i)
            agg[comp or group_of(op_class)] += ms
        # Attributed work covers cpu_op/user_annotation only, so what is left of
        # the layer frame is eager-dispatch and interpreter time. Name it.
        residual = layer["dur"] / 1000.0 - sum(agg.values())
        if residual > 0:
            agg["unattributed"] += residual
        out.append((layer["dur"] / 1000.0, agg))
    return out


def layer_profile(events, annotation, items, args) -> dict | None:
    win, parent = step_window(events, annotation)
    layers = layer_frames(win, annotation["ts"], annotation["ts"] + annotation["dur"],
                          re.compile(args.layer_regex))
    if len(layers) < 2:
        return None
    per_layer = attribute_layers(layers, win, label_resolver(win, parent), items)
    return {
        "n_layers": len(per_layer),
        "layer_ms": [t for t, _ in per_layer],
        "components": [dict(c) for _, c in per_layer],
    }


def mean_components(prof: dict, skip_first: bool = True) -> dict[str, float]:
    rows = prof["components"][1:] if skip_first else prof["components"]
    acc: collections.Counter = collections.Counter()
    for row in rows:
        acc.update(row)
    return {k: v / len(rows) for k, v in acc.items()}


def report(path: str, args) -> dict | None:
    events = at.complete_events(at.load(path))
    if not events:
        print(f"skip {os.path.basename(path)}: no complete events", file=sys.stderr)
        return None
    kind = at.kind_of_trace(events)
    metric = "device_ms" if kind == "gpu" else "self_ms"

    steps = sorted(
        (e for e in events if e.get("cat") == "user_annotation" and STEP_RE.match(e["name"])),
        key=lambda e: e["ts"],
    )
    print(f"\n=== {os.path.basename(path)}  [{kind} trace, op metric={metric}] ===")
    if not steps:
        print(
            "  no execute_context_* annotations: this is not an engine-core trace\n"
            "  (frontend/driver traces have no step boundary; use analyze_trace.py)."
        )
        return None

    t0 = min(e["ts"] for e in events)
    t1 = max(e["ts"] + e["dur"] for e in events)
    print(f"  steps: {len(steps)}   trace window: {(t1 - t0) / 1e6:.2f} s")

    header = (
        f"  {'#':>2} {'ctx req/tok':>13} {'gen req/tok':>12} {'tokens':>6} "
        f"{'state':>6} {'prepare':>7} {'forward':>9} {'logits':>7} "
        f"{'sample':>7} {'sched':>6} {'post':>6} {'step':>8} {'gap':>6}"
    )
    print("\n-- engine steps --")
    print(header)
    rows = []
    for i, a in enumerate(steps):
        end = a["ts"] + a["dur"]
        nxt = steps[i + 1]["ts"] if i + 1 < len(steps) else end
        cq, ct, gq, gt = (int(x) for x in STEP_RE.match(a["name"]).groups())
        inside = phase_ms(events, PHASES, a["ts"], end)
        gap = (
            phase_ms(events, GAP_PHASES, end, nxt)
            if nxt > end
            else dict.fromkeys(GAP_PHASES, 0.0)
        )
        rows.append(
            dict(
                step=i, cq=cq, ct=ct, gq=gq, gt=gt, tokens=ct + gt,
                step_ms=a["dur"] / 1000.0, gap_ms=(nxt - end) / 1000.0,
                **inside, **gap,
            )
        )
        print(
            f"  {i:>2} {cq:5d}/{ct:<7d} {gq:5d}/{gt:<6d} {ct + gt:>6d} "
            f"{inside['state']:6.2f} {inside['prepare']:7.2f} {inside['forward']:9.1f} "
            f"{inside['logits']:7.2f} {gap['sample']:7.2f} {gap['schedule']:6.2f} "
            f"{gap['postproc']:6.2f} {a['dur'] / 1000.0:8.1f} {(nxt - end) / 1000.0:6.2f}"
        )

    # --- per-layer anatomy of one prefill step and one decode step -----------
    self_ms = at.self_times(events)
    items = work_items(events, kind, self_ms, args.vocab_size)
    prefill = next((r for r in rows if r["cq"] and not r["gq"] and r["tokens"] > 1), None)
    decodes = [r for r in rows if not r["cq"] and r["gq"]]
    decode = decodes[0] if decodes else None

    profile: dict = {"steps": rows, "layers": {}}
    for label, row in (("decode", decode), ("prefill", prefill)):
        if row is None:
            continue
        prof = layer_profile(events, steps[row["step"]], items, args)
        if prof is not None:
            profile["layers"][label] = prof

    print("\n-- per-layer anatomy --")
    print(f"  {'component':13s} {'decode ms':>10s} {'%':>6s} {'prefill ms':>11s} {'%':>6s}")
    means = {lab: mean_components(p) for lab, p in profile["layers"].items()}
    # Denominator: the attributed total, not the layer frame. On CPU the two are
    # the same (unattributed fills the gap), but on a GPU the device work queued
    # inside a layer can outlast the host span that launched it -- so dividing
    # device ms by host ms during prefill gave percentages summing past 100.
    totals = {lab: sum(m.values()) for lab, m in means.items()}
    keys = [k for k in COMPONENT_ORDER if any(k in m for m in means.values())]
    keys += sorted({k for m in means.values() for k in m} - set(keys))
    for k in keys:
        cells = []
        for lab in ("decode", "prefill"):
            if lab not in means:
                cells += ["", ""]
                continue
            ms = means[lab].get(k, 0.0)
            pct = 100 * ms / totals[lab] if totals[lab] else 0.0
            cells += [f"{ms:10.2f}", f"{pct:6.1f}"]
        print(f"  {k:13s} " + " ".join(cells))
    if not means:
        print("  (no decoder-layer frames matched --layer-regex)")

    print(
        "\n  unattributed = layer frame time with no cpu_op/kernel under it. On CPU that is\n"
        "  eager dispatch and interpreter time; on GPU it is launch gaps and device idle,\n"
        "  where the per-op numbers are device time and can exceed the host span."
    )

    print()
    for lab in ("decode", "prefill"):
        if lab not in profile["layers"]:
            continue
        p = profile["layers"][lab]
        row = (decode if lab == "decode" else prefill)
        n = p["n_layers"]
        excl = sum(p["layer_ms"][1:]) / (n - 1)
        incl = sum(p["layer_ms"]) / n
        print(
            f"  {lab}: {n} layers, {excl:.2f} ms/layer excluding L0 "
            f"({incl:.2f} including it) = {sum(p['layer_ms']):.1f} ms of the "
            f"{row['forward']:.1f} ms forward"
        )

    # Layer 0 is not noise, so show what it is instead of hiding it.
    if "decode" in profile["layers"] and len(profile["layers"]["decode"]["layer_ms"]) > 1:
        p0 = profile["layers"]["decode"]
        l0 = p0["layer_ms"][0]
        others = sum(p0["layer_ms"][1:]) / (p0["n_layers"] - 1)
        print(
            f"  layer 0: {l0:.2f} ms vs {others:.2f} ms for the other {p0['n_layers'] - 1} "
            f"({l0 / others:.2f}x) on the first decode step"
        )
        if len(decodes) > 1:
            last = layer_profile(events, steps[decodes[-1]["step"]], items, args)
            if last and len(last["layer_ms"]) > 1:
                ratio = last["layer_ms"][0] / last["layer_ms"][1]
                print(
                    f"           by step {decodes[-1]['step']}: {last['layer_ms'][0]:.2f} ms vs "
                    f"{last['layer_ms'][1]:.2f} ms for L1 "
                    f"({ratio:.2f}x) -- "
                    + ("back to baseline" if ratio < 1.15 else "penalty still decaying")
                )
        why = (
            "oneDNN builds a primitive for the new M"
            if kind == "cpu"
            else "first launch of these shapes on the device"
        )
        print(
            "           -> one-time per-shape setup on the first step at the new shape "
            f"({why}).\n"
            "              It does not recur, so the means above exclude L0 and report "
            "it here instead."
        )

    # --- what the scheduler decided -----------------------------------------
    ctx_steps = [r for r in rows if r["cq"]]
    print("\n-- scheduler decisions in this window --")
    print(
        f"  steps with prefill work: {len(ctx_steps)} of {len(rows)}; "
        f"prefills co-scheduled: {', '.join(str(r['cq']) for r in ctx_steps) or 'none'}"
    )
    print(
        f"  context tokens: {sum(r['ct'] for r in rows)}   "
        f"generation tokens: {sum(r['gt'] for r in rows)}   "
        f"model time: {sum(r['step_ms'] for r in rows) / 1000:.2f} s"
    )
    glue = sum(r["sample"] + r["schedule"] + r["postproc"] for r in rows)
    model = sum(r["step_ms"] for r in rows)
    print(
        f"  engine-core glue (sample+schedule+postproc): {glue:.1f} ms over {len(rows)} "
        f"steps = {glue / len(rows):.2f} ms/step, "
        f"{100 * glue / (glue + model):.1f}% of engine-core time"
    )
    return profile


def figure(profiles: dict[str, dict], out: str) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    tags = [t for t, p in profiles.items() if p and p.get("layers")]
    if not tags:
        return
    panels = [lab for lab in ("decode", "prefill") if any(lab in profiles[t]["layers"] for t in tags)]
    if not panels:
        return
    order = [
        k
        for k in COMPONENT_ORDER
        if any(k in c for t in tags for p in panels for c in profiles[t]["layers"][p]["components"])
    ]
    cmap = plt.get_cmap("tab20")
    colors = {k: cmap(i % 20) for i, k in enumerate(order)}

    fig, axes = plt.subplots(1, len(panels), figsize=(6.4 * len(panels), 5.6))
    if len(panels) == 1:
        axes = [axes]
    for ax, panel in zip(axes, panels):
        tags_here = [t for t in tags if panel in profiles[t]["layers"]]
        n = max(profiles[t]["layers"][panel]["n_layers"] for t in tags_here)
        width = 0.8 / len(tags_here)
        for j, tag in enumerate(tags_here):
            comps = profiles[tag]["layers"][panel]["components"]
            y = [i - 0.4 + width / 2 + j * width for i in range(len(comps))]
            left = [0.0] * len(comps)
            for k in order:
                vals = [c.get(k, 0.0) for c in comps]
                ax.barh(y, vals, height=width, left=left, color=colors[k],
                        label=k if j == 0 else None)
                left = [a + b for a, b in zip(left, vals)]
        ax.set_yticks(range(n))
        ax.set_yticklabels([f"L{i}" for i in range(n)], fontsize=6)
        ax.invert_yaxis()
        ax.set_xlabel("ms in this layer")
        row = next(
            (r for t in tags_here for r in profiles[t]["steps"]
             if (panel == "decode") == (not r["cq"]) and (panel == "decode" or r["tokens"] > 1)),
            None,
        )
        prof = profiles[tags_here[0]]["layers"][panel]
        per_layer = sum(prof["layer_ms"][1:]) / (prof["n_layers"] - 1)
        toks = f"{row['tokens']} tokens" if row else "?"
        ax.set_title(f"{panel}: {toks} through {prof['n_layers']} layers "
                     f"({per_layer:.1f} ms/layer)", fontsize=10)
        ax.grid(axis="x", alpha=0.3)
    axes[-1].legend(bbox_to_anchor=(1.02, 1), loc="upper left", fontsize=8, frameon=False)
    fig.suptitle(
        "One engine step, layer by layer: prefill runs every layer wide, decode runs every "
        "layer narrow", fontsize=11
    )
    fig.tight_layout()
    fig.savefig(out, dpi=160, bbox_inches="tight")
    plt.close(fig)
    print(f"\nwrote {out}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--trace", required=True, nargs="+", help="trace paths or globs")
    ap.add_argument("--vocab-size", type=int, default=151936)
    ap.add_argument("--layer-regex", default=r"DecoderLayer_(\d+)")
    ap.add_argument("--fig", default=None, help="write a per-layer figure here")
    args = ap.parse_args()

    paths = [p for pat in args.trace for p in sorted(glob.glob(pat))]
    if not paths:
        print("no traces matched", file=sys.stderr)
        return 1
    profiles = {}
    for p in paths:
        tag = re.sub(r"\.pt\.trace\.json(\.gz)?$", "", os.path.basename(p))
        profiles[tag] = report(p, args)
    if args.fig:
        figure(profiles, args.fig)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
