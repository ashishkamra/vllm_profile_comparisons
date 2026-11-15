# 2. Methodology: what makes this comparison honest

A CPU-vs-GPU comparison is easy to run and easy to get wrong. These are the rules this
repo follows, and the threats that remain.

## Rules

**R1 — Same vLLM commit, verified not assumed.**
Both legs run `4ba787ec99` (`v0.33.0`). `harness/collect_env.sh` records
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

The default scenario set is `sync` and `conc8`. `sweep` is opt-in because its throughput
probe has to complete a round of requests inside one window: at concurrency 16 with 128
output tokens that is ~16 x 128 / throughput seconds, and on the CPU leg (13 s per
512/128 request) the probe finished 0 of 16 requests in 30 s. That dragged its measured
rate to 0.00 req/s and pushed the interpolated ladder down to 0.03-0.06 req/s — where the
server is idle between requests and most points complete 0-2 requests — so GuideLLM
raised `Cannot compile GenerativeMetrics: No measurement start or end times available`.
The run is preserved in
[`results/evidence/failed-sweep-cpu-ref.txt`](../results/evidence/failed-sweep-cpu-ref.txt).
Both legs run the same default set, so this does not disturb R4.

**R5 — Same server knobs.**
`--max-model-len 2048 --max-num-seqs 16` on both. GPU adds
`--gpu-memory-utilization 0.85` (8 GB card); the CPU leg pins
`VLLM_CPU_KVCACHE_SPACE=4` (GiB, vLLM's documented default) and
`OMP_NUM_THREADS` = the host's **physical** core count (14 here, via `lscpu`,
not `nproc`, which follows the affinity mask and can change between runs) instead. Anything that changes scheduling behaviour must be
identical, or the traces measure different schedulers. The `sweep` profile's
`max_concurrency` is capped at `max_num_seqs` (16): GuideLLM defaults to 512, which on a
16-slot server measures queueing rather than the model, on both legs alike. Preemption
counts are scraped
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

**R9 — Op-cost profiles keep the CPU worker inside the engine core, and are bounded by engine iterations.**
`vllm serve` always runs the API server and the EngineCore as separate processes
(`AsyncLLM` has no in-process mode). On the GPU the model worker runs inside the EngineCore.
On the CPU, vLLM's default moves it into a third, worker process (`platforms/cpu.py:351`),
and only the API server and the worker carry a torch profiler, so the scheduler is not
traced. The op-cost runs (`profile_run.sh`, `profile_scenario.sh`) set
`VLLM_ENABLE_V1_MULTIPROCESSING=0` (`WORKER_IN_ENGINE=1` in `harness/serve.sh`), which keeps
the CPU worker in the EngineCore so one trace holds scheduler, runner and model. That changes
the CPU's process layout. The request-journey runs (`harness/journey_run.sh`, mode
`default`) keep vLLM's real layout and sample the CPU engine core with py-spy instead. An
earlier version of this rule called the runs “single-process”; they never were, see
`report/index.html#corrections`.

The profiler is also capped with `/start_profile {"max_iterations": N}` (default 20)
rather than by wall clock. Reaching the cap stops the profiler and writes the trace *on the
engine's main thread*, which freezes the engine for as long as that takes (15 s in one CPU
run), so the cap should sit above the steps of interest and `/stop_profile` should be called
after the requests finish. An unbounded concurrent profile writes a trace large enough
that `/stop_profile` times out on the RPC and the trace is never flushed; capping by
iterations makes the trace size independent of scenario duration and keeps the profiled
window comparable across scenarios and variants. Both legs use the same cap, so op *counts*
are comparable too (`112` linears per iteration on the CPU int8 leg, i.e. `2240` scaled_mm
calls over 20 iterations, and `560` calls of each individual projection).

The window starts at `/start_profile`, i.e. while GuideLLM's first requests are still being
admitted, so it contains the initial prefill burst and then steady-state decode. That mix
is a property of the scenario, is identical on both legs, and is why the profile is read as
"composition of the scenario window" rather than "decode-only cost". Per-request TTFT and
ITL from `benchmarks.json` are the decode/prefill separation; the trace is the composition.

**R10 — Every quoted mean carries its n.**
GuideLLM computes its metrics over *completed* requests only. A duration-bounded scenario
therefore leaves requests incomplete at the window edge (`conc8` on the CPU leg: 8 completed
of 16 made for fp32, 15 of 24 for INT8), and `results` never mixes them into the means.
`harness/summarize.py` copies `request_totals` next to every mean, so an incomplete
fraction is always visible instead of being inferred.

Numbers are read from `benchmarks.json`, not from GuideLLM's console summary: for the same
run, 0.8.1's console table reported 15 completed requests where its own JSON records 8
successful + 8 incomplete. The console is a log; the JSON is the artifact.

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

### One run per point, so no between-run error bars
Each scenario is measured once. The `p95` columns in `results/summary.csv` are
*distribution within a run* (over its requests or its inter-token intervals), not a
confidence interval across runs; nothing here repeats a scenario to estimate run-to-run
variance. The INT8 sync point was measured twice on separate server boots and moved by
<1 % on throughput (19.29 -> 19.22 output tok/s, 0.151 -> 0.150 req/s), which is the only
evidence about repetition in this repo. Treat differences under a few percent as noise,
which is why the reported INT8 gain is stated to two significant figures rather than
three.

### Host-specific ISA build (CPU leg)
The CPU extension is compiled with `-march=native`, so the built `.so` uses every
extension this host has (here `-march=alderlake`: AVX2, AVX-VNNI, GFNI, VAES, BMI2, FMA).
It is therefore only valid on the build host, and the numbers are a statement about *this*
CPU. Two consequences: (a) do not ship that wheel anywhere else; (b) if the CPU leg is
ever rebuilt on different hardware, the profile changes for reasons unrelated to vLLM.

`harness/probe_isa.sh` records what actually runs rather than trusting the flags, because
those are three different claims:

| claim | evidence |
|---|---|
| the build emitted native instructions | `_C_AVX2.abi3.so` contains `shlx`/`sarx`/`rorx` (BMI2) and `vfmadd*` (FMA); a stock `-mavx2 -mf16c` build emits none of them |
| vLLM dispatches to the AVX2 variant | `/proc/self/maps` shows `_C_AVX2.abi3.so` mapped and `_C.abi3.so` (the AVX-512/AMX variant, named `_C`) not; `torch.cpu._is_avx512_supported()` is `False` |
| the int8 GEMM uses AVX-VNNI | oneDNN with `DNNL_VERBOSE=1` reports `isa:Intel AVX2 with Intel DL Boost` and picks `matmul,brg_matmul:avx2_vnni` for the s8×s8 GEMM |

That last one matters most: on this host the int8 linear hot path is oneDNN (the faster
`check_cpu_sgl_kernel` path needs AMX), and oneDNN selects its kernel from CPUID at
runtime. Compiled-in VNNI would prove nothing on its own.
`harness/collect_env.sh` records the flag expansion and `harness/probe_isa.sh` the runtime
dispatch; both are redirected into `results/`.

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
