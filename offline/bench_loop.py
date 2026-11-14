# SPDX-License-Identifier: Apache-2.0
"""Steady-state loop for external sampling profilers (py-spy / perf).

Runs generation continuously for --seconds so the sampler gets a long,
representative window.
"""

import argparse
import time

from vllm import LLM, SamplingParams

p = argparse.ArgumentParser()
p.add_argument("--model", default="Qwen/Qwen3-0.6B")
p.add_argument("--dtype", default="float32")
p.add_argument("--mode", choices=["decode", "prefill"], default="decode")
p.add_argument("--seconds", type=float, default=20.0)
p.add_argument("--batch", type=int, default=2)
p.add_argument("--prose", default="/tmp/opencode/vllm-prof/prose.txt")
args = p.parse_args()

padding = " ".join(open(args.prose).read().split())

llm = LLM(model=args.model, dtype=args.dtype, max_model_len=2048, enforce_eager=True)

if args.mode == "decode":
    prompts = ["Hi"] * args.batch
    n_tokens = 64
else:
    prompts = [(padding + " ") * 6 + "The capital of France is"] * args.batch
    n_tokens = 1

params = SamplingParams(temperature=0.0, max_tokens=n_tokens, ignore_eos=True)
llm.generate(prompts, params, use_tqdm=False)  # warmup

t_end = time.perf_counter() + args.seconds
iters = tokens = 0
t0 = time.perf_counter()
while time.perf_counter() < t_end:
    out = llm.generate(prompts, params, use_tqdm=False)
    iters += 1
    tokens += sum(len(o.outputs[0].token_ids) for o in out)
dur = time.perf_counter() - t0
print(f"{args.mode}: {iters} iters, {tokens} tokens, {tokens / dur:.1f} tok/s, {dur:.1f}s")
