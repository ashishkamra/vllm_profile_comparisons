# 2. Methodology: what makes this comparison honest

A CPU-vs-GPU comparison is easy to run and easy to get wrong. These are the rules this
repo follows, and the threats that remain.

## Rules

**R1 — Same vLLM commit, verified not assumed.**
Both legs run `7833dce847` (`v0.34.0rc0`). `harness/collect_env.sh` records
`git rev-parse HEAD` and `vllm.__version__`; the report quotes them. The CPU leg *must*
build from source (`VLLM_TARGET_DEVICE=cpu` — no CPU wheels are published); the GPU leg
may use a precompiled wheel. Record which, per leg.

**R2 — Device-optimal precision, with a reference run on each device.**

| leg | artifact | compute | role |
|---|---|---|---|
| CPU | `Qwen/Qwen3-0.6B` | fp32 | reference (what the math "is") |
| CPU | `nytopop/Qwen3-0.6B.w8a8` | INT8 W8A8 | deployment-shaped |
| GPU | `Qwen/Qwen3-0.6B` | bf16 | reference |
| GPU | `RedHatAI/Qwen3-0.6B-FP8-dynamic` | FP8 W8A8 | deployment-shaped |

Compare quantized-to-quantized **and** reference-to-reference. Never compare the CPU
reference against the GPU quantized run and call the difference "the device".

**R3 — KV cache dtype is held constant across the comparison.**
Quantize weights and activations, not the cache: keep `--kv-cache-dtype auto`
(fp16/bf16-class) on both. Otherwise the decode profile mixes two changes and the memory
bandwidth story stops being interpretable.

**R4 — Identical workload.**
GuideLLM `0.8.1`, identical `--data` (prompt/output token counts), identical
`--constraint`, identical `--profile`. Scenario ids in `harness/run_scenario.sh` are the
single source of truth for both legs. Token counts are *synthetic and exact*, so
`prompt_tokens` must not be re-tuned per device.

**R5 — Same server knobs.**
`--max-model-len 2048 --max-num-seqs 16` on both. GPU adds
`--gpu-memory-utilization 0.85` (8 GB card); the CPU leg pins
`VLLM_CPU_KVCACHE_SPACE=4` (GiB, vLLM's documented default) and
`OMP_NUM_THREADS=$(nproc)` instead. Anything that changes scheduling behaviour must be
identical, or the traces measure different schedulers. Preemption counts are scraped
around each run (`run_scenario.sh`): a non-zero delta marks the result invalid rather
than letting a scheduler artefact be reported as a hardware difference.

**R6 — Profiling runs are separate from throughput runs, and eager.**
`--enforce-eager` for every profiled run on GPU: CUDA graph capture turns N kernel
launches into one replay node, which destroys op-level attribution. Headline throughput
numbers are taken *without* `--enforce-eager` and reported separately, because eager mode
understates GPU performance. Both numbers are published; they answer different questions.

The profile also sets `torch_profiler_record_shapes=true`. It is **off** by default at
v0.33.0 and costs some overhead, but without input shapes every projection collapses into
one `linear` bucket: the analyzer cannot separate `qkv`, `o_proj`, `gate_up`, `down` or
`lm_head`, which is half of what the comparison is for. It is enabled identically on both
legs, so the overhead is symmetric.

**R7 — Same model runner on both legs.**
v0.33.0 has two model runners (see `docs/01-architecture.md`). Triton is disabled on a
CPU-only host, so auto-selection would put the GPU on V2 and the CPU on V1. Both legs are
pinned with `VLLM_USE_V2_MODEL_RUNNER=0` (V1). Recorded in `collect_env.sh` output; a run
whose runner differs from the other leg is not comparable.

**R8 — Fixtures before interpretation.**
Each scenario is run once warm (prefix cache warm, allocator warm) and then measured.
`/reset_prefix_cache` between runs so run N is not accelerated by run N-1.

## Threats to validity (stated, not hidden)

### Quantization asymmetry
INT8 W8A8 and FP8 E4M3 are different numerics with different hardware support. This
study asks *"how does each device spend its time when deployed the way you would actually
deploy it"*, not *"which device is faster at identical arithmetic"*. The reference runs
exist to bound the difference: `fp32 CPU vs bf16 GPU` is the like-for-like-ish baseline,
`int8 CPU vs fp8 GPU` is the deployment comparison.

Numerical quality is **not** measured here beyond a smoke check. If the quantized runs
produce degenerate output the profile is meaningless, so `harness/smoke_check.py` compares
greedy outputs of each artifact against the reference on a handful of prompts.

### No-AMX CPU
Alder Lake consumer silicon has AVX-VNNI but no AVX-512 and no AMX. Consequences that must
be stated whenever CPU INT8 numbers are quoted:
- the fast `check_cpu_sgl_kernel` path requires `torch.cpu._is_amx_tile_supported()`, so
  INT8 linear falls back to oneDNN;
- bf16 has no hardware path and is emulated, which is why the CPU reference is fp32;
- a Sapphire Rapids or Emerald Rapids CPU would show a different INT8 profile. These
  numbers characterise *this* CPU, not "CPU" as a category.

### Hybrid-core topology
12th-gen mobile parts mix P-cores and E-cores. `OMP_NUM_THREADS` pins the thread count;
it does not control *which* cores. Record `OMP_NUM_THREADS`, and keep it fixed across
scenarios. Do not "tune" it per run.

### Host-specific ISA build (CPU leg)
The CPU extension is compiled with `-march=native`, so the built `.so` uses every
extension this host has (here `-march=alderlake`: AVX2, AVX-VNNI, GFNI, VAES, BMI2, FMA).
It is therefore only valid on the build host, and the numbers are a statement about *this*
CPU. Two consequences: (a) do not ship that wheel anywhere else; (b) if the CPU leg is
ever rebuilt on different hardware, the profile changes for reasons unrelated to vLLM.
`results/env-*.txt` records the expansion.

### Host CPU on the GPU leg
The 4060's host CPU does tokenization, scheduling, detokenization, and kernel launches.
At 0.6B scale, GPU step time is small enough that host overhead is visible in TTFT and in
the low-batch-rate profile. That is a real finding, not noise — but it is a property of the
*host*, and the report says which host it was.

### Async device time
On GPU, host spans and device kernels are on different clocks: `execute_model` returns
before the work finishes. Summing `torch.profiler` self time per op is therefore *not*
wall time. The analyzer therefore reports two things for GPU traces:
- **device time** per stage (kernel durations, correlated to the launching CPU op), which
  is what you want for "where the FLOPs went";
- **host time** per stage, which is what you want for "where the Python went".
For CPU traces the two coincide, which is exactly why the CPU profile is the easier one to
learn on.

### Concurrency and the KV budget
8 GB with a 0.6B model is comfortable, but `conc8` and `sweep` can still exhaust the block
pool if `max_num_seqs` or `max_model_len` are raised. Preemption would then dominate the
profile (scheduler work, recompute) and the comparison would be about the block manager.
`harness/run_scenario.sh` records preemption counters alongside every result; if the count
is non-zero the run is marked `preempt>0` and excluded from cross-device arithmetic.

## Anti-patterns this repo avoids

- Comparing a GPU with CUDA graphs against a CPU without them, then attributing the gap
  to "kernel efficiency".
- Comparing fp32 CPU against fp8 GPU and calling it a hardware comparison.
- Profiling a cold run (JIT, allocator growth, first-touch pages).
- Quoting a single throughput number from a sweep without the concurrency it came from.
- Reporting `torch.profiler` per-op sums as wall time on an async device.
