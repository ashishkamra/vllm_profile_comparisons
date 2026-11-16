#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Drive a small, deterministic request pattern and record each token's arrival.

This is the load for the "journey" profiles: runs that exist to show how vLLM
moves a request through its processes, not to measure throughput. Two things
make it different from drive_requests.py:

* Streaming. Every token travels engine core -> API server -> client as its own
  chunk, so the frontend's per-token path (output handler, detokenizer, SSE) is
  exercised the way a chat client exercises it.
* Wall-clock timestamps. torch.profiler stamps every process's trace on the same
  clock (`baseTimeNanoseconds` + `ts` is Unix time), so the client's send time,
  first token and last token can be placed on the same timeline as the API
  server and engine-core traces.

Scenarios:

  single     one throwaway request (absorbs the profiler's start-up cost, which
             lands on the first request after /start_profile), then one 512/32
             request: the request whose journey is followed.
  staggered  four requests whose arrivals are triggered by tokens received, not
             by seconds, so the same engine steps see the same batch on a fast
             and a slow device. Shows admission into a running batch, chunked
             prefill, a prefix-cache hit (D repeats A's prompt), and requests
             finishing while others continue.

    drive_journey.py --target http://127.0.0.1:8000 --model Qwen/Qwen3-0.6B \\
                     --scenario single --out results/journey/cpu-default-single.client.json
"""

from __future__ import annotations

import argparse
import json
import threading
import time
import urllib.request

FILLER = (
    "The quick brown fox jumps over the lazy dog while the scheduler batches "
    "requests and the model runner launches its kernels. "
)


def post(target: str, path: str, body: dict, timeout: float) -> dict:
    req = urllib.request.Request(
        f"{target}{path}",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


def build_prompt(target: str, model: str, n_tokens: int, preamble: str, timeout: float) -> tuple[str, int]:
    """Text of exactly n_tokens under the server's own tokenizer (see drive_requests.py)."""
    text = preamble + FILLER * (n_tokens // 8 + 8)
    ids = post(target, "/tokenize", {"model": model, "prompt": text}, timeout)["tokens"]
    if len(ids) > n_tokens:
        text = post(target, "/detokenize", {"model": model, "tokens": ids[:n_tokens]}, timeout)["prompt"]
        ids = post(target, "/tokenize", {"model": model, "prompt": text}, timeout)["tokens"]
    return text, len(ids)


def build_ids(target: str, model: str, n_tokens: int, preamble: str, timeout: float) -> list[int]:
    """Exactly n_tokens token IDs. Sent as a token-ID prompt, so concatenating two
    of them gives a sequence whose boundaries are exact: a 384-token prefix is
    exactly 3 CPU blocks (128) or 24 GPU blocks (16), whatever the text."""
    text = preamble + FILLER * (n_tokens // 8 + 8)
    ids = post(target, "/tokenize", {"model": model, "prompt": text, "add_special_tokens": False}, timeout)["tokens"]
    assert len(ids) >= n_tokens, (len(ids), n_tokens)
    return ids[:n_tokens]


class Progress:
    """Tokens received per request, so one request can wait for another's k-th token."""

    def __init__(self) -> None:
        self._tokens: dict[str, int] = {}
        self._cv = threading.Condition()

    def bump(self, name: str) -> None:
        with self._cv:
            self._tokens[name] = self._tokens.get(name, 0) + 1
            self._cv.notify_all()

    def wait_for(self, name: str, k: int, timeout: float) -> None:
        with self._cv:
            if not self._cv.wait_for(lambda: self._tokens.get(name, 0) >= k, timeout):
                raise TimeoutError(f"{name} did not reach {k} tokens in {timeout}s")


def stream_one(target: str, model: str, spec: dict, progress: Progress, timeout: float) -> dict:
    rec = {
        "name": spec["name"],
        "role": spec.get("role", "measured"),
        "prompt_tokens": spec["prompt_len"],
        "max_tokens": spec["max_tokens"],
        "trigger": spec.get("trigger"),
        "token_times": [],
    }
    body = {
        "model": model,
        "prompt": spec["prompt"],
        "max_tokens": spec["max_tokens"],
        "temperature": 0.0,
        "ignore_eos": True,  # identical decode length on both devices
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    req = urllib.request.Request(
        f"{target}/v1/completions",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    rec["t_send"] = time.time()
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        rec["t_headers"] = time.time()
        for raw in resp:
            line = raw.decode().strip()
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                break
            chunk = json.loads(payload)
            rec.setdefault("request_id", chunk.get("id"))
            if chunk.get("usage"):
                rec["usage"] = chunk["usage"]
            # One choice per chunk = one engine step's token for this request.
            # The usage-only final chunk has no choices. usage.completion_tokens
            # cross-checks the count.
            for _choice in chunk.get("choices") or []:
                rec["token_times"].append(time.time())
                progress.bump(spec["name"])
    rec["t_done"] = time.time()
    tt = rec["token_times"]
    if tt:
        rec["ttft_ms"] = round((tt[0] - rec["t_send"]) * 1e3, 2)
        if len(tt) > 1:
            gaps = [b - a for a, b in zip(tt, tt[1:])]
            rec["itl_mean_ms"] = round(sum(gaps) / len(gaps) * 1e3, 2)
    return rec


def scenario_specs(name: str) -> list[dict]:
    """Request specs; `trigger` = (other request, tokens it must have received)."""
    if name == "single":
        return [
            dict(name="absorb", role="absorb", preamble="Absorb the profiler start-up. ",
                 prompt_tokens=32, max_tokens=4),
            dict(name="R", preamble="", prompt_tokens=512, max_tokens=32,
                 trigger=("absorb", "done")),
        ]
    if name == "staggered":
        return [
            dict(name="absorb", role="absorb", preamble="Absorb the profiler start-up. ",
                 prompt_tokens=32, max_tokens=4),
            dict(name="A", preamble="Request A. ", prompt_tokens=512, max_tokens=24,
                 trigger=("absorb", "done")),
            dict(name="B", preamble="Request B. ", prompt_tokens=1024, max_tokens=16,
                 trigger=("A", 4)),
            dict(name="C", preamble="Request C. ", prompt_tokens=256, max_tokens=16,
                 trigger=("A", 8)),
            # Same text as A: its full KV blocks are already cached by now.
            dict(name="D", same_prompt_as="A", prompt_tokens=512, max_tokens=8,
                 trigger=("A", 14)),
        ]
    if name == "prefix":
        # KV-cache sharing. P1..P4 share a 384-token prefix (sent as token IDs).
        # P2 runs alongside P1: both block tables point at the same physical
        # prefix blocks. P3 arrives after both finished: the prefix blocks are
        # free but still cached. P4 repeats P1's whole prompt.
        sys_prompt = ("Shared system prompt. ", 384)
        return [
            dict(name="absorb", role="absorb", preamble="Absorb the profiler start-up. ",
                 prompt_tokens=32, max_tokens=4),
            dict(name="P1", prefix=sys_prompt, suffix=("First user turn. ", 128),
                 max_tokens=16, trigger=("absorb", "done")),
            dict(name="P2", prefix=sys_prompt, suffix=("Second user turn. ", 128),
                 max_tokens=16, trigger=("P1", 4)),
            dict(name="P3", prefix=sys_prompt, suffix=("Third user turn. ", 128),
                 max_tokens=8, trigger=("P2", "done")),
            dict(name="P4", prefix=sys_prompt, suffix=("First user turn. ", 128),
                 max_tokens=4, trigger=("P3", "done")),
        ]
    if name == "pressure":
        # KV-cache pressure. journey_run.sh shrinks the pool to 2,176 tokens: four
        # 448-token prompts fit, four 576-token sequences do not, so the
        # scheduler has to preempt while they decode.
        specs = [dict(name="absorb", role="absorb", preamble="Absorb the profiler start-up. ",
                      prompt_tokens=32, max_tokens=4)]
        for i in range(1, 5):
            specs.append(dict(name=f"Q{i}", prefix=(f"Request Q{i}, its own text. ", 448),
                              max_tokens=128, trigger=("absorb", "done")))
        return specs
    raise SystemExit(f"unknown scenario {name}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", default="http://127.0.0.1:8000")
    ap.add_argument("--model", required=True)
    ap.add_argument("--scenario", choices=["single", "staggered", "prefix", "pressure"], required=True)
    ap.add_argument("--timeout", type=float, default=900.0)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    specs = scenario_specs(args.scenario)
    prompts: dict[str, tuple[str, int]] = {}
    for s in specs:
        if "prefix" in s:
            ids = build_ids(args.target, args.model, s["prefix"][1], s["prefix"][0], args.timeout)
            if s.get("suffix"):
                ids = ids + build_ids(args.target, args.model, s["suffix"][1], s["suffix"][0], args.timeout)
            prompts[s["name"]] = (ids, len(ids))
        elif "same_prompt_as" in s:
            prompts[s["name"]] = prompts[s["same_prompt_as"]]
        else:
            prompts[s["name"]] = build_prompt(
                args.target, args.model, s["prompt_tokens"], s["preamble"], args.timeout
            )
        s["prompt"], s["prompt_len"] = prompts[s["name"]]

    progress = Progress()
    done: dict[str, threading.Event] = {s["name"]: threading.Event() for s in specs}
    results: dict[str, dict] = {}
    errors: list[str] = []

    def run(spec: dict) -> None:
        try:
            trig = spec.get("trigger")
            if trig:
                other, k = trig
                if k == "done":
                    done[other].wait(args.timeout)
                    time.sleep(0.5)  # a visible idle gap in the trace between the two
                else:
                    progress.wait_for(other, k, args.timeout)
            results[spec["name"]] = stream_one(args.target, args.model, spec, progress, args.timeout)
        except Exception as e:  # report, do not hang the other threads
            errors.append(f"{spec['name']}: {e!r}")
        finally:
            done[spec["name"]].set()

    threads = [threading.Thread(target=run, args=(s,), name=s["name"]) for s in specs]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    out = {
        "scenario": args.scenario,
        "model": args.model,
        "clock": "unix seconds (time.time); same clock as trace baseTimeNanoseconds+ts",
        "requests": [results[s["name"]] for s in specs if s["name"] in results],
        "errors": errors,
    }
    with open(args.out, "w") as f:
        json.dump(out, f, indent=2)
    for r in out["requests"]:
        print(
            f"{r['name']:>6}: prompt {r['prompt_tokens']:>5}  tokens {len(r['token_times']):>3}  "
            f"ttft {r.get('ttft_ms', float('nan')):>9.1f} ms  itl {r.get('itl_mean_ms', float('nan')):>7.1f} ms"
        )
    if errors:
        print("errors:", *errors, sep="\n  ")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
