#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Quality smoke check: is a quantized artifact producing sane text?

Not an eval. It exists so that a fast profile is never reported for an artifact whose
output is degenerate. Compare each quantized variant against the reference artifact of
the same device (or against the other device, tolerantly).

    smoke_check.py --target http://127.0.0.1:8000 --model Qwen/Qwen3-0.6B \
                   --out results/smoke-cpu-ref.json
    smoke_check.py --target http://127.0.0.1:8000 --model nytopop/Qwen3-0.6B.w8a8 \
                   --out results/smoke-cpu-int8.json --baseline results/smoke-cpu-ref.json
"""

from __future__ import annotations

import argparse
import json
import urllib.request

PROMPTS = [
    "The capital of France is",
    "def fibonacci(n):",
    "Explain in one sentence why the sky is blue:",
    "A list of three prime numbers:",
    "The opposite of 'hot' is",
]


def complete(target: str, model: str, prompt: str, max_tokens: int, temperature: float) -> dict:
    body = json.dumps(
        {
            "model": model,
            "prompt": prompt,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "ignore_eos": True,
        }
    ).encode()
    req = urllib.request.Request(
        f"{target}/v1/completions", data=body, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.load(resp)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", default="http://127.0.0.1:8000")
    ap.add_argument("--model", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--baseline", default=None)
    ap.add_argument("--max-tokens", type=int, default=32)
    ap.add_argument("--temperature", type=float, default=0.0)
    args = ap.parse_args()

    results = {}
    for p in PROMPTS:
        r = complete(args.target, args.model, p, args.max_tokens, args.temperature)
        results[p] = r["choices"][0]["text"]

    with open(args.out, "w") as f:
        json.dump({"model": args.model, "temperature": args.temperature, "outputs": results}, f, indent=2)
    print(f"wrote {args.out}")

    if args.baseline:
        with open(args.baseline) as f:
            base = json.load(f)["outputs"]
        for p in PROMPTS:
            a, b = base[p], results[p]
            same = "SAME" if a == b else "DIFF"
            print(f"\n[{same}] {p}")
            print(f"  ref: {a[:160]!r}")
            print(f"  new: {b[:160]!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
