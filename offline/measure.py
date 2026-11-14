# SPDX-License-Identifier: Apache-2.0
"""Plain (uninstrumented) timing of a small model on the vLLM CPU backend.

Splits the two very different phases so we have numbers to compare the
profiler against:
  * prefill-bound : many prompt tokens, 1 output token
  * decode-bound  : tiny prompt, many output tokens
"""

import argparse
import time

from vllm import LLM, SamplingParams

p = argparse.ArgumentParser()
p.add_argument("--model", required=True)
p.add_argument("--dtype", default="float32")
p.add_argument("--max-model-len", type=int, default=2048)
p.add_argument("--repeats", type=int, default=3)
p.add_argument("--prose", default=None, help="file with a paragraph to pad prompts")
args = p.parse_args()

padding = ""
if args.prose:
    with open(args.prose) as f:
        padding = " ".join(f.read().split())

llm = LLM(
    model=args.model,
    dtype=args.dtype,
    max_model_len=args.max_model_len,
    enforce_eager=True,
    disable_log_stats=False,
)

def run(prompt: str, max_tokens: int, n: int = 1):
    params = SamplingParams(temperature=0.0, max_tokens=max_tokens, ignore_eos=True)
    return llm.generate([prompt] * n, params, use_tqdm=False)

# ---------------- warmup (kernel selection, caches, allocator) --------------
prefill_prompt = (padding + " ") * 6 + "The capital of France is"
run(prefill_prompt, 8)
run("Hi", 8)

out = {}

# ---------------- prefill-bound --------------------------------------------
toks = len(llm.get_tokenizer().encode(prefill_prompt))
run(prefill_prompt, 1)  # warm the exact shape
t0 = time.perf_counter()
for _ in range(args.repeats):
    o = run(prefill_prompt, 1)
t1 = time.perf_counter()
pre_s = (t1 - t0) / args.repeats
out["prefill"] = {
    "prompt_tokens": toks,
    "seconds": round(pre_s, 4),
    "tok_per_s": round(toks / pre_s, 1),
}
print("PREFILL", out["prefill"])

# ---------------- decode-bound ---------------------------------------------
short = "Hi"
n_out = 128
run(short, 16)
t0 = time.perf_counter()
o = run(short, n_out, n=2)
t1 = time.perf_counter()
dec_s = (t1 - t0)
generated = sum(len(x.outputs[0].token_ids) for x in o)
out["decode"] = {
    "generated_tokens": generated,
    "seconds": round(dec_s, 4),
    "ms_per_token_per_seq": round(1000 * dec_s / (generated / 2), 2),
    "tok_per_s": round(generated / dec_s, 1),
}
print("DECODE", out["decode"])

with open("/tmp/opencode/vllm-prof/baseline.json", "w") as f:
    import json

    json.dump(out, f, indent=2)
