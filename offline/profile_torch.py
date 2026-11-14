# SPDX-License-Identifier: Apache-2.0
"""Run the vLLM CPU backend under torch.profiler and dump a Chrome trace.

Writes tensorboard-style traces (*.pt.trace.json.gz) into /tmp/opencode/vllm-prof/traces.
"""

import argparse
import os
import time

from vllm import LLM, SamplingParams
from vllm.config import ProfilerConfig

TRACE_DIR = "/tmp/opencode/vllm-prof/traces"

p = argparse.ArgumentParser()
p.add_argument("--model", required=True)
p.add_argument("--dtype", default="float32")
p.add_argument("--phase", choices=["prefill", "decode"], default="decode")
p.add_argument("--max-model-len", type=int, default=2048)
p.add_argument("--delay", type=int, default=1)
p.add_argument("--active", type=int, default=3)
p.add_argument("--prose", default=None)
args = p.parse_args()

os.makedirs(TRACE_DIR, exist_ok=True)

padding = ""
if args.prose:
    with open(args.prose) as f:
        padding = " ".join(f.read().split())

llm = LLM(
    model=args.model,
    dtype=args.dtype,
    max_model_len=args.max_model_len,
    enforce_eager=True,  # no torch.compile fusion: keep the profile readable
    profiler_config=ProfilerConfig(
        profiler="torch",
        torch_profiler_dir=TRACE_DIR,
        torch_profiler_activities=["CPU"],
        torch_profiler_with_stack=True,  # attribute time to Python frames
        torch_profiler_record_shapes=False,
        torch_profiler_use_gzip=True,
    ),
)

if args.phase == "prefill":
    prompt = (padding + " ") * 6 + "The capital of France is"
    max_tokens = 1
else:
    prompt = "Hi"
    max_tokens = 48

params = SamplingParams(temperature=0.0, max_tokens=max_tokens, ignore_eos=True)

# warmup outside the profiled region
llm.generate([prompt], params, use_tqdm=False)

t0 = time.perf_counter()
llm.start_profile(
    profile_prefix=f"cpu-{args.phase}",
    delay_iterations=args.delay,
    max_iterations=args.active,
)
llm.generate([prompt] * 2, params, use_tqdm=False)
llm.stop_profile()
print(f"[{args.phase}] profiled wall time {time.perf_counter() - t0:.3f}s")
print("traces in", TRACE_DIR)
