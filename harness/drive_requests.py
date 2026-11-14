#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Send a fixed, serial request set into a running server.

This is the load for profiling runs, and it is deliberately not GuideLLM. A
throughput measurement needs sustained concurrency; a *profile* does not -- the
trace is bounded by engine iterations (docs/02-methodology.md R9), so all the
load has to do is produce those iterations. GuideLLM's open-loop profile instead
runs 8 streams for a full minute and spawns a request-loader process that
prefetches thousands of samples, which on a laptop GPU means sustained maximum
heat for a window the profiler has long stopped recording.

Requests go out one at a time, in a fixed order, with ignore_eos so that every
device decodes exactly the same number of tokens. That makes the two legs
comparable request-for-request rather than merely similar.

    drive_requests.py --target http://127.0.0.1:8000 --model Qwen/Qwen3-0.6B \
                      --requests 4 --max-tokens 128 --prompt-tokens 512 \
                      --out results/cpu-ref-profile.json
"""

from __future__ import annotations

import argparse
import json
import time
import urllib.request

# A long, boring text. It is only a vehicle for tokens: content does not matter,
# but it must be identical on both legs, which is why it is generated here and
# not sampled from a dataset.
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


def build_prompt(
    target: str, model: str, prompt_tokens: int, timeout: float, preamble: str = ""
) -> tuple[str, int]:
    """Return (text, token count).

    The count is measured with the server's own tokenizer rather than estimated,
    so the prefill size in the trace can be checked against what was asked for
    instead of assumed. When the text overshoots, it is trimmed through the
    server's tokenizer so both legs prefill the same number of tokens.

    `preamble` exists for the warmup request: it changes the text from the very
    first token, so the warmup cannot be a cached prefix of the study prompt.
    Without it a 32-token warmup shares its first two blocks with the 512-token
    prompt, and if /reset_prefix_cache does not take effect (it is best-effort
    in profile_run.sh) the profiled prefill silently schedules 480 tokens
    instead of 512 -- which is exactly what happened on the GPU leg.
    """
    text = preamble + FILLER * (prompt_tokens // 8 + 8)
    ids = post(target, "/tokenize", {"model": model, "prompt": text}, timeout)["tokens"]

    if len(ids) > prompt_tokens:
        trimmed = post(target, "/detokenize", {"model": model, "tokens": ids[:prompt_tokens]}, timeout)
        text = trimmed["prompt"]
        # Round-tripping can merge or split at the cut, so measure again.
        ids = post(target, "/tokenize", {"model": model, "prompt": text}, timeout)["tokens"]
    return text, len(ids)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", default="http://127.0.0.1:8000")
    ap.add_argument("--model", required=True)
    ap.add_argument("--requests", type=int, default=4)
    ap.add_argument("--max-tokens", type=int, default=128)
    ap.add_argument("--prompt-tokens", type=int, default=512)
    ap.add_argument(
        "--preamble",
        default="",
        help="text placed before the filler; the warmup uses a unique one so its "
        "tokens can never be a cached prefix of the timed prompt",
    )
    ap.add_argument("--timeout", type=float, default=600.0)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    prompt, prompt_len = build_prompt(
        args.target, args.model, args.prompt_tokens, args.timeout, args.preamble
    )
    print(f"prompt: {prompt_len} tokens (asked for {args.prompt_tokens}); "
          f"{args.requests} requests, {args.max_tokens} tokens each, serial")

    records = []
    for i in range(args.requests):
        started = time.monotonic()
        resp = post(
            args.target,
            "/v1/completions",
            {
                "model": args.model,
                "prompt": prompt,
                "max_tokens": args.max_tokens,
                "temperature": 0.0,
                # Without this the two legs stop at different EOS positions and
                # decode step counts stop being comparable.
                "ignore_eos": True,
            },
            args.timeout,
        )
        elapsed = time.monotonic() - started
        usage = resp.get("usage") or {}
        rec = {
            "index": i,
            "seconds": round(elapsed, 3),
            "prompt_tokens": usage.get("prompt_tokens"),
            "completion_tokens": usage.get("completion_tokens"),
        }
        records.append(rec)
        print(json.dumps(rec))

    summary = {
        "model": args.model,
        "requests": args.requests,
        "prompt_tokens_requested": args.prompt_tokens,
        "prompt_tokens_actual": prompt_len,
        "max_tokens": args.max_tokens,
        "serial": True,
        "records": records,
        "total_seconds": round(sum(r["seconds"] for r in records), 3),
    }
    if args.out:
        with open(args.out, "w") as f:
            json.dump(summary, f, indent=2)
        print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
