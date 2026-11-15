#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Follow requests through vLLM's processes, from the traces of one journey run.

A journey run (harness/journey_run.sh) leaves, per device and mode:

  * the API server's torch trace      (*.async_llm.*.pt.trace.json.gz)
  * the engine core's or the worker's torch trace (rank0.*.pt.trace.json.gz)
  * on the CPU in default mode, a py-spy sample of the engine core, which has no
    torch profiler there (enginecore.pyspy.chrometrace.json)
  * the client's per-token arrival times and a manifest of process roles

All of them are on one clock: torch stamps `baseTimeNanoseconds`, and
baseTimeNanoseconds/1e3 + ts is Unix time in microseconds, which is also what
the client recorded. This module puts them on one timeline and answers three
questions with it:

  1. Where is the request at each moment between "client sends" and "client
     receives the first token"?  -> milestones()
  2. What does each process and thread do during one engine step?  -> lanes()
  3. What did the scheduler put in each step?  -> step_table()

and writes a merged trace you can open in https://ui.perfetto.dev to browse the
same thing yourself, with flow arrows along the request's path.

    journey.py --run cpu-eager-single            # milestones + figures + merged trace
    journey.py --run all
"""

from __future__ import annotations

import argparse
import glob
import gzip
import json
import os
import re
import sys
from dataclasses import dataclass, field

R = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TRACES = os.path.join(R, "traces", "journey")
RESULTS = os.path.join(R, "results", "journey")
FIGURES = os.path.join(R, "figures", "journey")

STEP_RX = re.compile(r"execute_context_(\d+)\((\d+)\)_generation_(\d+)\((\d+)\)")


# --------------------------------------------------------------------------
# loading
# --------------------------------------------------------------------------
@dataclass
class Proc:
    role: str                 # api_server | engine_core | worker | gpu
    pid: object
    events: list[dict]        # complete events, ts/dur in absolute microseconds
    thread_names: dict = field(default_factory=dict)
    source: str = "torch"     # torch | py-spy


@dataclass
class Run:
    tag: str
    manifest: dict
    client: dict
    procs: dict[str, Proc]

    @property
    def device(self) -> str:
        return self.manifest["device"]

    def proc(self, role: str) -> Proc | None:
        return self.procs.get(role)

    def all_events(self, roles=None):
        for role, p in self.procs.items():
            if roles is None or role in roles:
                yield from p.events

    def request(self, name: str) -> dict:
        return next(r for r in self.client["requests"] if r["name"] == name)


def _load_torch(path: str) -> tuple[list[dict], list[dict], float]:
    with gzip.open(path, "rt") as f:
        raw = json.load(f)
    base = raw.get("baseTimeNanoseconds", 0) / 1e3
    xs, meta = [], []
    for e in raw["traceEvents"]:
        ph = e.get("ph")
        if ph == "X" and "ts" in e:
            e["ts"] = e["ts"] + base
            xs.append(e)
        elif ph == "M":
            meta.append(e)
    return xs, meta, base


def _load_pyspy(path: str, t0_unix: float) -> list[dict]:
    """py-spy chrometrace (B/E pairs, ts relative to its start) -> complete events."""
    with open(path) as f:
        raw = json.load(f)
    out, stacks = [], {}
    for e in raw:
        tid = e.get("tid")
        args = e.get("args") or {}
        # py-spy names frames by qualified name ("EngineCore.step",
        # "MessageQueue._wait_until_readable.<locals>.check_readable") and the
        # line being executed; reduce to torch's "file(line): func" shape so the
        # same patterns classify both.
        func = str(e.get("name", "")).split(".")[-1]
        name = f"{args.get('filename', '?')}({args.get('line', 0)}): {func}"
        if e.get("ph") == "B":
            stacks.setdefault(tid, []).append((name, e["ts"]))
        elif e.get("ph") == "E" and stacks.get(tid):
            n, ts = stacks[tid].pop()
            out.append({"ph": "X", "cat": "py-spy", "name": n, "pid": e.get("pid"),
                        "tid": tid, "ts": t0_unix * 1e6 + ts, "dur": e["ts"] - ts})
    return out


def load_run(tag: str) -> Run:
    d = os.path.join(TRACES, tag)
    manifest = json.load(open(os.path.join(RESULTS, f"{tag}.manifest.json")))
    client = json.load(open(os.path.join(RESULTS, f"{tag}.client.json")))
    procs: dict[str, Proc] = {}

    for path in glob.glob(os.path.join(d, "*.pt.trace.json.gz")):
        xs, meta, _ = _load_torch(path)
        pname = next((m["args"]["name"] for m in meta if m.get("name") == "process_name"
                      and isinstance(m.get("pid"), int) and m["pid"] > 0), "")
        tnames = {m["tid"]: m["args"]["name"] for m in meta if m.get("name") == "thread_name"}
        if "async_llm" in os.path.basename(path):
            role = "api_server"
        elif "Worker" in pname:
            role = "worker"
        else:
            role = "engine_core"
        host = [e for e in xs if e.get("cat") not in ("kernel", "gpu_memcpy", "gpu_memset", "gpu_user_annotation")]
        dev = [e for e in xs if e.get("cat") in ("kernel", "gpu_memcpy", "gpu_memset")]
        host_pid = next((e["pid"] for e in host if isinstance(e.get("pid"), int) and e["pid"] > 0), None)
        procs[role] = Proc(role, host_pid, host, tnames)
        if dev:
            # the launcher events stay with the host process; kernels get their own lane
            procs["gpu"] = Proc("gpu", "gpu", dev, {})
            procs["gpu"].launchers = {e["args"].get("correlation"): e for e in host
                                      if e.get("cat") in ("cuda_runtime", "cuda_driver")}

    spy = os.path.join(d, "enginecore.pyspy.chrometrace.json")
    if os.path.exists(spy) and "engine_core" not in procs and manifest.get("t_pyspy_start"):
        evs = _load_pyspy(spy, manifest["t_pyspy_start"])
        evs = _align_pyspy(evs, procs.get("worker"))
        procs["engine_core"] = Proc("engine_core", manifest["process_pids"].get("EngineCore"),
                                    evs, {}, source="py-spy")
    return Run(tag, manifest, client, procs)


def _align_pyspy(evs: list[dict], worker: Proc | None) -> list[dict]:
    """Refine py-spy's clock against the worker trace.

    py-spy stamps samples relative to its own start, which we only know to within
    its attach time (10-30 ms here). In the CPU's three-process layout the engine
    core submits each RPC and then blocks in `get_response` until the worker
    writes the reply into the shared-memory queue, so the end of every long wait
    coincides with a worker `enqueue_output`. Shift by the median difference.
    """
    if worker is None:
        return evs
    ends = sorted(e["ts"] + e["dur"] for e in evs
                  if e["name"].endswith(": get_response") and e["dur"] > 20000)
    enq = sorted(e["ts"] for e in worker.events if e["name"].endswith(": enqueue_output"))
    if not ends or not enq:
        return evs
    diffs = sorted(min(enq, key=lambda w: abs(w - x)) - x for x in ends)
    off = diffs[len(diffs) // 2]
    if abs(off) < 2e6:
        for e in evs:
            e["ts"] += off
    return evs


# --------------------------------------------------------------------------
# threads -> roles
# --------------------------------------------------------------------------
def thread_roles(p: Proc) -> dict:
    """tid -> lane label, found from the frames each thread runs."""
    cached = getattr(p, "_roles", None)
    if cached is not None:
        return cached
    p._roles = _thread_roles(p)
    return p._roles


def _thread_roles(p: Proc) -> dict:
    by_tid: dict = {}
    for e in p.events:
        by_tid.setdefault(e.get("tid"), []).append(e["name"])
    roles = {}
    for tid, names in by_tid.items():
        blob = "\n".join(names[:200000])
        if p.role == "api_server":
            if "_encode_plus" in blob or "encode_batch" in blob:
                roles[tid] = "tokenizer thread"
            elif tid == p.pid or "uvicorn" in blob:
                roles[tid] = "main (asyncio)"
        elif p.role == "engine_core":
            if "process_input_sockets" in blob:
                roles[tid] = "input thread (ZMQ in)"
            elif "process_output_sockets" in blob:
                roles[tid] = "output thread (ZMQ out)"
            elif "run_busy_loop" in blob:
                roles[tid] = "main (busy loop)"
        elif p.role == "worker":
            if "worker_busy_loop" in blob or tid == p.pid:
                roles[tid] = "main (worker loop)"
    return roles


# --------------------------------------------------------------------------
# activity categories (what a lane is doing)
# --------------------------------------------------------------------------
CATS = [
    # (category, regex on frame name). Painted outer-first, so inner frames win.
    ("http", r"httptools_impl\.py\(\d+\): (run_asgi|data_received)|fastapi/routing|create_completion"),
    ("tokenize", r"_encode_plus|encode_batch|tokenize_prompt_async"),
    ("add_request", r"async_llm\.py\(\d+\): (add_request|_add_request)|input_processor\.py\(\d+\): process_inputs|core\.py\(\d+\): (_handle_client_request|preprocess_add_request)"),
    ("ipc", r"_send_msg_tracking_payload|core_client\.py\(\d+\): (add_request_async|process_outputs_socket)|serial_utils\.py\(\d+\): (decode|encode)|shm_broadcast\.py\(\d+\): (enqueue|notify)|enqueue_output"),
    ("output", r"async_llm\.py\(\d+\): output_handler|output_processor\.py\(\d+\): process_outputs|detokenizer\.py|httptools_impl\.py\(\d+\): send"),
    ("schedule", r"scheduler\.py\(\d+\): schedule$"),
    ("prepare", r"gpu_model_runner\.py\(\d+\): (_update_states|_prepare_inputs|_build_attention_metadata)"),
    ("forward", r"gpu_model_runner\.py\(\d+\): _model_forward"),
    ("logits", r"compute_logits"),
    ("sample", r"gpu_model_runner\.py\(\d+\): sample_tokens"),
    ("update", r"scheduler\.py\(\d+\): update_from_output"),
    ("wait", r"queue\.py\(\d+\): get$|shm_broadcast\.py\(\d+\): (dequeue|acquire_read|_wait_until_readable|wait|check_readable)|sched_yield|get_response|_wait_for_response|threading\.py\(\d+\): wait|selectors\.py\(\d+\): select|<built-in method (poll|select|epoll|control) of"),
    ("wait_gpu", r"cudaEventSynchronize|cudaStreamSynchronize|cudaDeviceSynchronize|get_output$"),
]
CAT_RX = [(c, re.compile(rx)) for c, rx in CATS]
CAT_COLOR = {
    "http": "#4394e5", "tokenize": "#9b8ad4", "add_request": "#c9a3ff", "ipc": "#F4B400",
    "output": "#7fc8f8", "schedule": "#2fc4c4", "prepare": "#5ba84f", "forward": "#EE0000",
    "logits": "#ff8a8a", "sample": "#f6cd62", "update": "#8cbdf0", "wait": "#3a3a3a",
    "wait_gpu": "#6b6b6b",
    "k:gemm": "#EE0000", "k:attention": "#F4B400", "k:norm/fused": "#2fc4c4",
    "k:memcpy": "#8cbdf0", "k:other": "#9a9a9a",
}
CAT_LABEL = {
    "http": "HTTP / OpenAI layer", "tokenize": "tokenize", "add_request": "add request",
    "ipc": "IPC (ZMQ / shared memory)", "output": "output path (detokenize, stream)",
    "schedule": "schedule()", "prepare": "prepare inputs", "forward": "model forward",
    "logits": "compute_logits (lm_head)", "sample": "sample", "update": "update_from_output",
    "wait": "blocked / waiting", "wait_gpu": "waiting for GPU",
    "k:gemm": "GPU: GEMM / GEMV", "k:attention": "GPU: attention / KV write",
    "k:norm/fused": "GPU: norm / fused (Triton)", "k:memcpy": "GPU: memcpy", "k:other": "GPU: other",
}


def category(name: str) -> str | None:
    for c, rx in CAT_RX:
        if rx.search(name):
            return c
    return None


def kernel_category(e: dict) -> str:
    n = e["name"]
    if e.get("cat") in ("gpu_memcpy", "gpu_memset") or "memcpy" in n.lower():
        return "k:memcpy"
    if re.search(r"gemm|gemv|cutlass|sm\d+_xmma|ampere_|cublas", n, re.I):
        return "k:gemm"
    if re.search(r"flash|attention|reshape_and_cache", n, re.I):
        return "k:attention"
    if re.search(r"norm|triton_|rotary|act_and_mul|silu", n, re.I):
        return "k:norm/fused"
    return "k:other"


def paint(events: list[dict], lo: float, hi: float) -> list[tuple[float, float, str]]:
    """Intervals (start, end, category) for one thread, innermost category winning."""
    cands = []
    for e in events:
        if e["ts"] >= hi or e["ts"] + e.get("dur", 0) <= lo:
            continue
        c = category(e["name"])
        if c:
            cands.append((e["ts"], e["ts"] + e.get("dur", 0), c))
    # Sweep: at every point the innermost (shortest) active frame decides the
    # category. Frames on one thread nest, so the shortest active one is the
    # deepest. O(n log n) with a heap and lazy deletion.
    import heapq
    bounds = []
    for idx, (a, b, c) in enumerate(cands):
        a, b = max(a, lo), min(b, hi)
        if b > a:
            bounds.append((a, 0, idx, b - a, c))
            bounds.append((b, 1, idx, b - a, c))
    bounds.sort(key=lambda x: (x[0], x[1]))
    heap: list = []
    ended: set = set()
    segs: list[tuple[float, float, str]] = []
    prev_t = None
    for t, kind, idx, dur, c in bounds:
        while heap and heap[0][1] in ended:
            heapq.heappop(heap)
        if prev_t is not None and heap and t > prev_t:
            cat = heap[0][2]
            if segs and segs[-1][2] == cat and abs(segs[-1][1] - prev_t) < 1e-6:
                segs[-1] = (segs[-1][0], t, cat)
            else:
                segs.append((prev_t, t, cat))
        if kind == 0:
            heapq.heappush(heap, (dur, idx, c))
        else:
            ended.add(idx)
        prev_t = t
    return segs


def lanes(run: Run, lo: float, hi: float) -> list[tuple[str, list[tuple[float, float, str]]]]:
    out = []
    order = [("api_server", ["main (asyncio)", "tokenizer thread"]),
             ("engine_core", ["input thread (ZMQ in)", "main (busy loop)", "output thread (ZMQ out)"]),
             ("worker", ["main (worker loop)"])]
    proc_label = {"api_server": "API server", "engine_core": "EngineCore", "worker": "Worker"}
    for role, wanted in order:
        p = run.proc(role)
        if p is None:
            continue
        roles = thread_roles(p)
        for lane in wanted:
            tids = [t for t, r in roles.items() if r == lane]
            if not tids:
                continue
            evs = [e for e in p.events if e.get("tid") in tids]
            src = " (py-spy)" if p.source == "py-spy" else ""
            out.append((f"{proc_label[role]}{src} · {lane}", paint(evs, lo, hi)))
    g = run.proc("gpu")
    if g:
        segs = [(max(e["ts"], lo), min(e["ts"] + e["dur"], hi), kernel_category(e))
                for e in g.events if e["ts"] < hi and e["ts"] + e["dur"] > lo]
        out.append(("GPU · kernels", sorted(segs)))
    return out


# --------------------------------------------------------------------------
# steps and milestones
# --------------------------------------------------------------------------
def steps(run: Run) -> list[dict]:
    src = run.proc("engine_core") if run.proc("engine_core") and run.proc("engine_core").source == "torch" else run.proc("worker")
    ann = sorted((e for e in src.events if e.get("cat") == "user_annotation" and STEP_RX.match(e["name"])),
                 key=lambda e: e["ts"])
    out = []
    for i, a in enumerate(ann):
        cq, ct, gq, gt = map(int, STEP_RX.match(a["name"]).groups())
        out.append({"i": i, "ts": a["ts"], "end": a["ts"] + a["dur"], "ctx_reqs": cq,
                    "ctx_tokens": ct, "gen_reqs": gq, "gen_tokens": gt, "name": a["name"]})
    return out


def _first(events, rx, after, before=float("inf"), end=False):
    rx = re.compile(rx)
    best = None
    for e in events:
        t = e["ts"] + (e.get("dur", 0) if end else 0)
        if after <= e["ts"] < before and rx.search(e["name"]):
            if best is None or e["ts"] < best["ts"]:
                best = e
    if best is None:
        return None
    return best["ts"] + (best.get("dur", 0) if end else 0)


def milestones(run: Run, name: str = "R") -> list[dict]:
    """Where request `name` is, from send to its first token, across processes."""
    req = run.request(name)
    api = run.proc("api_server").events
    ec = run.proc("engine_core")
    # py-spy samples every 2 ms: too coarse to place sub-millisecond milestones,
    # so a sampled engine core contributes lanes but not milestones.
    eng = ec.events if ec is not None and ec.source == "torch" else []
    wrk = run.proc("worker").events if run.proc("worker") else []
    model = wrk or eng  # where the model runs
    gpu = run.proc("gpu").events if run.proc("gpu") else []
    t = req["t_send"] * 1e6
    first_tok = req["token_times"][0] * 1e6
    ms = []

    def add(key, label, where, ts):
        if ts is not None:
            ms.append({"key": key, "label": label, "where": where, "t": ts})
        return ts

    add("client_send", "client sends the HTTP request", "client", t)
    cur = t
    for key, label, where, evs, rx, end in [
        ("http", "uvicorn parses the request, FastAPI routes it", "API server · main", api,
         r"httptools_impl\.py\(\d+\): (data_received|run_asgi)", False),
        ("create_completion", "OpenAI completions handler starts", "API server · main", api,
         r"api_router\.py\(\d+\): create_completion", False),
        ("tokenize", "prompt tokenized (HF fast tokenizer, helper thread)", "API server · tokenizer", api,
         r"_encode_plus", False),
        ("tokenize_end", "tokenization done", "API server · tokenizer", api, r"_encode_plus", True),
        ("add_request", "AsyncLLM.add_request: build EngineCoreRequest", "API server · main", api,
         r"async_llm\.py\(\d+\): add_request", False),
        ("zmq_send", "request sent to the engine over ZMQ", "API server · main", api,
         r"core_client\.py\(\d+\): add_request_async", True),
        ("engine_recv", "engine input thread decodes it into a Request", "EngineCore · input thread", eng,
         r"preprocess_add_request", False),
        ("sched_add", "Scheduler.add_request: request enters WAITING", "EngineCore · main", eng,
         r"scheduler\.py\(\d+\): add_request", False),
        ("schedule", "schedule(): request admitted, KV blocks allocated", "EngineCore · main", eng,
         r"scheduler\.py\(\d+\): schedule$", False),
    ]:
        v = _first(evs, rx, cur - 200, end=end)
        if v is not None and v >= cur - 200:
            add(key, label, where, v)
            cur = max(cur, v if not end else v)

    st = [s for s in steps(run) if s["ctx_tokens"] > 0 and s["ts"] >= cur - 1000]
    if st:
        s = st[0]
        if wrk:
            rpc = _first(wrk, r"multiproc_executor\.py\(\d+\): _execute_worker_rpc", s["ts"] - 50000, s["ts"])
            add("worker_rpc", "worker process picks up execute_model (shared-memory queue)", "Worker · main", rpc)
        add("step_start", f"prefill step starts ({s['ctx_tokens']} tokens)", "model runner", s["ts"])
        if gpu:
            k = min((e["ts"] for e in gpu if e["ts"] >= s["ts"]), default=None)
            add("gpu_first", "first GPU kernel of the step", "GPU", k)
        add("step_end", "step annotation ends (forward + logits done on host)", "model runner", s["end"])
        smp = _first(model, r"gpu_model_runner\.py\(\d+\): sample_tokens", s["ts"])
        add("sample", "sample_tokens: sampler picks the token", "model runner", smp)
        if gpu and smp:
            # kernels launched by this step's host ops: last one to finish
            lnch = run.proc("gpu").launchers
            ks = [e for e in gpu if (lnch.get(e["args"].get("correlation")) or {}).get("ts", 0) >= s["ts"]
                  and (lnch.get(e["args"].get("correlation")) or {}).get("ts", 0) <= smp + 2000]
            if ks:
                add("gpu_last", "last GPU kernel of the step finishes", "GPU", max(e["ts"] + e["dur"] for e in ks))
        wret = None
        if wrk and smp:
            wret = _first(wrk, r"multiproc_executor\.py\(\d+\): enqueue_output", smp)
            add("worker_return", "worker returns the sampled token (shared-memory queue)", "Worker · main", wret)
        upd = _first(eng, r"scheduler\.py\(\d+\): update_from_output", smp or s["end"])
        add("update", "update_from_output: token appended, stop checks", "EngineCore · main", upd)
        snd = _first(eng, r"_send_msg_tracking_payload", upd or s["end"])
        add("engine_send", "engine output thread sends EngineCoreOutputs (ZMQ)", "EngineCore · output thread", snd)
        base = snd or upd or wret or s["end"]
        rcv = _first(api, r"core_client\.py\(\d+\): process_outputs_socket", base - 2000)
        add("api_recv", "API server receives and decodes the outputs", "API server · main", rcv)
        po = _first(api, r"output_processor\.py\(\d+\): process_outputs", (rcv or base) - 100)
        add("process_outputs", "OutputProcessor: per-request state update", "API server · main", po)
        dt = _first(api, r"detokenizer\.py\(\d+\): update", (po or base) - 100)
        add("detokenize", "incremental detokenization", "API server · main", dt)
        hs = _first(api, r"httptools_impl\.py\(\d+\): send", (dt or base) - 100)
        add("http_send", "SSE chunk written to the socket", "API server · main", hs)
    add("client_first", "client receives the first token", "client", first_tok)
    ms.sort(key=lambda m: m["t"])
    for m in ms:
        m["ms_from_send"] = round((m["t"] - t) / 1e3, 3)
    return ms


def step_table(run: Run) -> list[dict]:
    """Per step: who got context tokens (inferred from arrival order), decodes, time."""
    reqs = [r for r in run.client["requests"]]
    pend = [[r["name"], r["prompt_tokens"], 0] for r in reqs]  # name, prompt, scheduled so far
    rows = []
    st = steps(run)
    for i, s in enumerate(st):
        owner, cached = None, None
        if s["ctx_tokens"]:
            while pend and pend[0][2] >= pend[0][1]:
                pend.pop(0)
            if pend:
                owner = pend[0][0]
                pend[0][2] += s["ctx_tokens"]
                nxt = st[i + 1] if i + 1 < len(st) else None
                done_by_count = pend[0][2] >= pend[0][1]
                if not done_by_count and nxt is not None and nxt["ctx_tokens"] == 0:
                    cached = pend[0][1] - pend[0][2]  # prefix-cache hit covered the rest
                    pend[0][2] = pend[0][1]
        rows.append({**s, "ctx_owner": owner, "cached_tokens": cached,
                     "dur_ms": round((s["end"] - s["ts"]) / 1e3, 2)})
    return rows


# --------------------------------------------------------------------------
# merged trace for Perfetto
# --------------------------------------------------------------------------
PROC_SORT = {"client": 1, "api_server": 2, "engine_core": 3, "worker": 4, "gpu": 5}
PROC_TITLE = {"client": "Client (load generator)", "api_server": "API server (AsyncLLM frontend)",
              "engine_core": "EngineCore (scheduler)", "worker": "Worker (model runner)",
              "gpu": "GPU (CUDA kernels)"}


def export_perfetto(run: Run, path: str) -> None:
    base = min(e["ts"] for e in run.all_events())
    out = []
    fake_pid = {"client": 1, "api_server": 2, "engine_core": 3, "worker": 4, "gpu": 5}
    for role, p in run.procs.items():
        pid = fake_pid[role]
        title = PROC_TITLE[role] + (" — sampled by py-spy" if p.source == "py-spy" else "") + f"  [pid {p.pid}]"
        out.append({"ph": "M", "name": "process_name", "pid": pid, "tid": 0, "args": {"name": title}})
        out.append({"ph": "M", "name": "process_sort_index", "pid": pid, "tid": 0, "args": {"sort_index": PROC_SORT[role]}})
        roles = thread_roles(p) if role != "gpu" else {}
        tids = {}
        for e in p.events:
            tid = e.get("tid")
            if tid not in tids:
                tids[tid] = len(tids) + 1
                label = roles.get(tid) or (f"stream {tid}" if role == "gpu" else f"thread {tid}")
                out.append({"ph": "M", "name": "thread_name", "pid": pid, "tid": tids[tid], "args": {"name": label}})
                out.append({"ph": "M", "name": "thread_sort_index", "pid": pid, "tid": tids[tid],
                            "args": {"sort_index": 0 if "main" in label else tids[tid]}})
        min_py = 2 if role == "api_server" else 20
        for e in p.events:
            c = e.get("cat")
            if c == "python_function" and e.get("dur", 0) < min_py:
                continue
            if c == "cpu_op" and e.get("dur", 0) < 5:
                continue
            ne = {k: v for k, v in e.items() if k in ("ph", "cat", "name", "dur", "args")}
            ne["ts"] = round(e["ts"] - base, 3)
            ne["pid"], ne["tid"] = pid, tids[e.get("tid")]
            if "args" in ne and c == "python_function":
                ne.pop("args")
            out.append(ne)
    # client lane: one track per request
    out.append({"ph": "M", "name": "process_name", "pid": 1, "tid": 0, "args": {"name": PROC_TITLE["client"]}})
    out.append({"ph": "M", "name": "process_sort_index", "pid": 1, "tid": 0, "args": {"sort_index": 1}})
    for i, r in enumerate(run.client["requests"]):
        tid = i + 1
        out.append({"ph": "M", "name": "thread_name", "pid": 1, "tid": tid, "args": {"name": f"request {r['name']}"}})
        t0 = r["t_send"] * 1e6 - base
        if r["token_times"]:
            t1 = r["token_times"][0] * 1e6 - base
            out.append({"ph": "X", "cat": "client", "name": f"{r['name']}: waiting for first token (TTFT)",
                        "pid": 1, "tid": tid, "ts": round(t0, 3), "dur": round(t1 - t0, 3)})
            out.append({"ph": "X", "cat": "client", "name": f"{r['name']}: streaming {len(r['token_times'])} tokens",
                        "pid": 1, "tid": tid, "ts": round(t1, 3), "dur": round(r["t_done"] * 1e6 - base - t1, 3)})
            for k, tt in enumerate(r["token_times"]):
                out.append({"ph": "i", "s": "t", "cat": "client", "name": f"token {k}", "pid": 1, "tid": tid,
                            "ts": round(tt * 1e6 - base, 3)})
    # Milestones of the measured request's first token, as numbered markers on
    # the process each one happens in: the path to follow when browsing.
    if any(r["name"] == "R" for r in run.client["requests"]):
        where_pid = {"client": 1, "API server": 2, "EngineCore": 3, "Worker": 4,
                     "model runner": 4 if run.proc("worker") else 3, "GPU": 5}
        for j, m in enumerate(milestones(run, "R")):
            pid = where_pid.get(m["where"].split(" · ")[0])
            if pid is None:
                continue
            out.append({"ph": "i", "s": "p", "cat": "journey", "pid": pid, "tid": 1,
                        "name": f"R{j:02d} {m['label']} (+{m['ms_from_send']} ms)",
                        "ts": round(m["t"] - base, 3)})
    with gzip.open(path, "wt") as f:
        json.dump({"traceEvents": out, "displayTimeUnit": "ms",
                   "otherData": {"journey": run.tag, "time_origin_unix_us": base}}, f)


# --------------------------------------------------------------------------
# figures
# --------------------------------------------------------------------------
def _style():
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    plt.rcParams.update({
        "figure.facecolor": "#151515", "axes.facecolor": "#1a1a1a", "savefig.facecolor": "#151515",
        "text.color": "#d2d2d2", "axes.labelcolor": "#d2d2d2", "xtick.color": "#a0a0a0",
        "ytick.color": "#d2d2d2", "axes.edgecolor": "#444444", "grid.color": "#2c2c2c",
        "font.size": 9, "axes.titlesize": 10.5, "axes.titleweight": "bold",
        "font.family": ["Red Hat Text", "DejaVu Sans"],
    })
    return plt


def draw_lanes(run: Run, lo: float, hi: float, title: str, out: str, marks=None, unit="ms",
               height_per_lane=0.42, note=None) -> None:
    plt = _style()
    from matplotlib.patches import Patch
    ls = lanes(run, lo, hi)
    # client lane on top
    client = []
    for r in run.client["requests"]:
        a = r["t_send"] * 1e6
        b = r["token_times"][0] * 1e6 if r["token_times"] else r["t_done"] * 1e6
        if b > lo and a < hi:
            client.append((max(a, lo), min(b, hi), "client_wait", r["name"]))
    scale = 1e3 if unit == "ms" else 1e6
    fig_h = 0.9 + height_per_lane * (len(ls) + 1) + 0.9
    fig, ax = plt.subplots(figsize=(11.5, fig_h))
    labels = ["Client"] + [l for l, _ in ls]
    n = len(labels)
    used = set()
    for a, b, _, nm in client:
        ax.broken_barh([((a - lo) / scale, (b - a) / scale)], (n - 1 - 0.32, 0.64),
                       facecolors="#2a3f55", edgecolors="#4394e5", linewidth=0.6)
        ax.text((a - lo) / scale, n - 1, f" {nm}: waiting for first token", va="center", fontsize=7.5, color="#d2d2d2", clip_on=True)
    for r in run.client["requests"]:
        for tt in r["token_times"]:
            x = tt * 1e6
            if lo <= x <= hi:
                ax.plot([(x - lo) / scale] * 2, [n - 1 - 0.32, n - 1 + 0.32], color="#f6cd62", lw=0.8)
    for i, (_, segs) in enumerate(ls):
        y = n - 2 - i
        by = {}
        for a, b, c in segs:
            if b - a <= 0:
                continue
            by.setdefault(c, []).append(((a - lo) / scale, (b - a) / scale))
            used.add(c)
        for c, bars in by.items():
            ax.broken_barh(bars, (y - 0.32, 0.64), facecolors=CAT_COLOR.get(c, "#888"), linewidth=0)
    if marks:
        for x, txt in marks:
            ax.axvline((x - lo) / scale, color="#ffffff", lw=0.6, ls=":", alpha=0.7)
            ax.text((x - lo) / scale, n - 0.35, txt, rotation=0, fontsize=7, color="#ffffff", ha="left", va="bottom")
    ax.set_yticks(range(n))
    ax.set_yticklabels(list(reversed(labels)), fontsize=8)
    ax.set_xlim(0, (hi - lo) / scale)
    ax.set_ylim(-0.6, n - 0.2 + (0.5 if marks else 0))
    ax.set_xlabel(f"{unit} from window start")
    ax.grid(axis="x", lw=0.5)
    ax.set_title(title, loc="left")
    order = [c for c in CAT_COLOR if c in used]
    handles = [Patch(color=CAT_COLOR[c], label=CAT_LABEL[c]) for c in order]
    handles.append(Patch(facecolor="#2a3f55", edgecolor="#4394e5", label="client: waiting"))
    ax.legend(handles=handles, loc="upper center", bbox_to_anchor=(0.5, -0.32 if n < 6 else -0.22),
              ncol=5, fontsize=7.2, frameon=False)
    if note:
        fig.text(0.01, 0.01, note, fontsize=7, color="#8a8a8a")
    fig.tight_layout()
    os.makedirs(os.path.dirname(out), exist_ok=True)
    fig.savefig(out, dpi=160)
    plt.close(fig)


def draw_steps(run: Run, out: str, title: str) -> None:
    plt = _style()
    rows = step_table(run)
    colors = {"absorb": "#555555", "A": "#4394e5", "B": "#EE0000", "C": "#5ba84f", "D": "#f6cd62", "R": "#4394e5"}
    fig, (ax, ax2) = plt.subplots(2, 1, figsize=(11.5, 5.6), sharex=True,
                                  gridspec_kw={"height_ratios": [3, 1.6]})
    for r in rows:
        x = r["i"]
        if r["ctx_tokens"]:
            ax.bar(x, r["ctx_tokens"], color=colors.get(r["ctx_owner"], "#888"), width=0.8)
            lbl = f"{r['ctx_owner']}\n{r['ctx_tokens']}"
            if r["cached_tokens"]:
                lbl += f"\n(+{r['cached_tokens']}\ncached)"
            ax.text(x, r["ctx_tokens"] + r["gen_tokens"] + 8, lbl, ha="center", va="bottom", fontsize=6.5)
        if r["gen_tokens"]:
            ax.bar(x, r["gen_tokens"], bottom=r["ctx_tokens"], color="#bbbbbb", width=0.8)
            if not r["ctx_tokens"]:
                ax.text(x, r["gen_tokens"] + 6, str(r["gen_tokens"]), ha="center", fontsize=6.5, color="#a0a0a0")
    budget = 512 if "staggered" in run.tag else None
    if budget:
        ax.axhline(budget, color="#F4B400", lw=0.8, ls="--")
        ax.text(len(rows) - 0.5, budget + 6, "token budget per step (--max-num-batched-tokens 512)",
                ha="right", fontsize=7, color="#F4B400")
    ax.set_ylabel("tokens scheduled")
    ax.set_ylim(0, 640)
    ax.set_xlim(-0.7, len(rows) - 0.3)
    ax.set_title(title, loc="left")
    from matplotlib.patches import Patch
    hs = [Patch(color=colors[k], label=f"prefill tokens: {k}") for k in ("A", "B", "C", "D")]
    hs.append(Patch(color="#555555", label="prefill: throwaway request"))
    hs.append(Patch(color="#bbbbbb", label="decode tokens (1 per running request)"))
    fig.legend(handles=hs, loc="lower center", fontsize=7.2, ncol=6, frameon=False)
    ax.grid(axis="y", lw=0.5)
    # how long each step took on the host (the annotation span)
    durs = [r["dur_ms"] for r in rows]
    ax2.bar([r["i"] for r in rows], durs, color=["#EE0000" if r["ctx_tokens"] else "#8a8a8a" for r in rows], width=0.8)
    for r in rows:
        if r["ctx_tokens"] or r["i"] % 4 == 1:
            ax2.text(r["i"], r["dur_ms"], f"{r['dur_ms']:.0f}" if r["dur_ms"] >= 10 else f"{r['dur_ms']:.1f}",
                     ha="center", va="bottom", fontsize=6.3, color="#d2d2d2")
    ax2.set_ylabel("step time, ms")
    ax2.set_ylim(0, max(durs) * 1.25)
    ax2.set_xlabel("engine step")
    ax2.grid(axis="y", lw=0.5)
    fig.tight_layout(rect=(0, 0.05, 1, 1))
    fig.savefig(out, dpi=160)
    plt.close(fig)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def process(tag: str) -> dict:
    print(f"== {tag}")
    run = load_run(tag)
    print("   processes:", {k: (v.pid, v.source, len(v.events)) for k, v in run.procs.items()})
    summary = {"tag": tag, "steps": step_table(run)}
    for s in summary["steps"]:
        s.pop("ts"), s.pop("end")
    if any(r["name"] == "R" for r in run.client["requests"]):
        ms = milestones(run, "R")
        summary["milestones_R"] = [{k: v for k, v in m.items() if k != "t"} for m in ms]
        for m in ms:
            print(f"   {m['ms_from_send']:10.3f} ms  {m['where']:28s} {m['label']}")
    os.makedirs(RESULTS, exist_ok=True)
    with open(os.path.join(RESULTS, f"{tag}.journey.json"), "w") as f:
        json.dump(summary, f, indent=1)
    p = os.path.join(TRACES, tag, "journey.perfetto.json.gz")
    export_perfetto(run, p)
    print(f"   merged trace: {os.path.relpath(p, R)} ({os.path.getsize(p) / 1e6:.1f} MB)")
    return summary


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", required=True, help="tag (e.g. cpu-eager-single) or 'all'")
    args = ap.parse_args()
    tags = sorted(os.path.basename(d) for d in glob.glob(os.path.join(TRACES, "*"))) if args.run == "all" else [args.run]
    for t in tags:
        process(t)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
