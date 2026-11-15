#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""The figures of the request-journey guide (report/index.html), from the journey runs.

Every figure is a window onto the same merged timeline that journey.py builds:
client, API server, engine core (or its py-spy sample), worker, GPU -- one
lane per thread that matters, coloured by what vLLM is doing in it.

    journey_figures.py            # writes figures/journey/*.png
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import functools  # noqa: E402

import journey as J  # noqa: E402

OUT = J.FIGURES
# Each run is drawn several times; load (and align) it once.
J.load_run = functools.lru_cache(maxsize=None)(J.load_run)


def ms_marks(run, keys):
    """Numbered markers for the milestones this run has (numbered without gaps)."""
    ms = {m["key"]: m for m in J.milestones(run, "R")}
    marks, legend = [], []
    for k in keys:
        if k in ms:
            n = len(marks) + 1
            marks.append((ms[k]["t"], str(n)))
            legend.append({"n": n, "key": k, "label": ms[k]["label"], "where": ms[k]["where"],
                           "ms_from_send": ms[k]["ms_from_send"]})
    return marks, legend


def window_request(run, name="R", pad=0.02):
    r = run.request(name)
    lo, hi = r["t_send"] * 1e6, r["t_done"] * 1e6
    span = hi - lo
    return lo - pad * span, hi + pad * span


def main() -> int:
    os.makedirs(OUT, exist_ok=True)
    legends = {}

    # 1. one whole request, real process layout
    for tag, unit, title in [
        ("cpu-default-single", "s", "CPU, vLLM defaults: one request (512 prompt / 32 output) through three processes"),
        ("gpu-default-single", "ms", "GPU, vLLM defaults: the same request through two processes and the GPU"),
        ("cpu-eager-single", "s", "CPU, eager, worker inside the engine core: the same request"),
        ("gpu-eager-single", "ms", "GPU, eager: the same request"),
    ]:
        run = J.load_run(tag)
        lo, hi = window_request(run)
        J.draw_lanes(run, lo, hi, title, os.path.join(OUT, f"j1-request-{tag}.png"), unit=unit)
        print("wrote", f"j1-request-{tag}.png")

    # 2. first token: inbound and outbound zooms
    for tag in ("cpu-eager-single", "gpu-default-single", "cpu-default-single", "gpu-eager-single"):
        run = J.load_run(tag)
        ms = {m["key"]: m for m in J.milestones(run, "R")}
        inbound = ["client_send", "http", "create_completion", "tokenize", "tokenize_end", "add_request",
                   "zmq_send", "engine_recv", "sched_add", "schedule", "worker_rpc", "step_start", "gpu_first"]
        marks, leg = ms_marks(run, inbound)
        lo = ms["client_send"]["t"] - 300
        hi = (ms.get("gpu_first") or ms["step_start"])["t"] + 1200
        J.draw_lanes(run, lo, hi, f"{tag}: from the client's send to the start of the prefill step",
                     os.path.join(OUT, f"j2-inbound-{tag}.png"), marks=marks, unit="ms")
        legends[f"j2-inbound-{tag}"] = leg
        outbound = ["sample", "gpu_last", "worker_return", "update", "engine_send", "api_recv",
                    "process_outputs", "detokenize", "http_send", "client_first"]
        marks, leg = ms_marks(run, outbound)
        lo = ms["sample"]["t"] - 1500
        hi = ms["client_first"]["t"] + 600
        J.draw_lanes(run, lo, hi, f"{tag}: from the sampler to the client's first token",
                     os.path.join(OUT, f"j3-outbound-{tag}.png"), marks=marks, unit="ms")
        legends[f"j3-outbound-{tag}"] = leg
        print("wrote", f"j2/j3 {tag}")

    # 3. decode steps, the four configurations side by side
    for tag, first, n, title in [
        ("cpu-eager-single", 8, 2, "CPU eager: two decode steps"),
        ("cpu-default-single", 8, 2, "CPU defaults (torch.compile, separate worker process): two decode steps"),
        ("gpu-eager-single", 8, 3, "GPU eager: three decode steps -- the host launches every kernel"),
        ("gpu-default-single", 7, 5, "GPU defaults: prefill, then decode steps replayed as CUDA graphs, scheduled one step ahead"),
    ]:
        run = J.load_run(tag)
        r = run.request("R")
        st = [s for s in J.steps(run) if s["ts"] >= r["t_send"] * 1e6]
        if tag == "gpu-default-single":
            # from the prefill: shows the host-launched prefill and the pipeline filling
            lo = st[0]["ts"] - 500
            hi = st[n]["end"] + 8000
        else:
            # n decode steps, starting at R's second decode (st[0] is the prefill)
            lo = st[2]["ts"] - 3000
            hi = st[2 + n]["ts"] + 2000
        J.draw_lanes(run, lo, hi, title, os.path.join(OUT, f"j4-decode-{tag}.png"), unit="ms")
        print("wrote", f"j4-decode-{tag}.png")

    # 4. the scheduler's decisions in the staggered scenario
    for tag, title in [
        ("cpu-eager-staggered", "CPU: what the scheduler put in each step (4 requests arriving while others decode)"),
        ("gpu-eager-staggered", "GPU: the same requests, the same scheduler"),
    ]:
        run = J.load_run(tag)
        J.draw_steps(run, os.path.join(OUT, f"j5-steps-{tag}.png"), title)
        print("wrote", f"j5-steps-{tag}.png")

    with open(os.path.join(J.RESULTS, "figure-milestones.json"), "w") as f:
        json.dump(legends, f, indent=1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
