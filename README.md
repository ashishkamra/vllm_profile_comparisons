# vLLM CPU vs GPU: where the time actually goes

A reproducible study of the **same model, same serving scenarios, two very different
accelerators**, with matched load generation (GuideLLM) and matched profiling, so the
latency numbers and the profiler traces can be read against each other.

The point is not "GPU is faster". It is: *the two devices spend their time in
different places, in ways you can see in a trace and predict from the hardware.*

| | CPU leg | GPU leg |
|---|---|---|
| Hardware | Intel i7-12800H (Alder Lake, AVX2 + AVX-VNNI, **no AVX-512, no AMX**) | NVIDIA RTX 4060 8 GB (Ada, sm_89) |
| vLLM | `v0.33.0` (`4ba787ec99`), built from source with `VLLM_TARGET_DEVICE=cpu` and `-march=native` | `vllm==0.33.0` from PyPI |
| Reference run | `Qwen/Qwen3-0.6B` @ fp32 | `Qwen/Qwen3-0.6B` @ bf16 |
| Quantized run | `nytopop/Qwen3-0.6B.w8a8` @ INT8 W8A8 | `RedHatAI/Qwen3-0.6B-FP8-dynamic` @ FP8 W8A8 |

Why those two quantized artifacts and not "fp8 on both": per
[`docs/features/quantization`](https://github.com/vllm-project/vllm/blob/main/docs/features/quantization/README.md),
FP8 W8A8 is Ada/Hopper-only and **not supported on x86 CPU**, while llm-compressor
INT8 W8A8 **is** supported on x86 CPU (via oneDNN when AMX is absent). INT8 is the
CPU-side analogue of FP8, not the same numerics — see
[`docs/02-methodology.md`](docs/02-methodology.md#quantization-asymmetry).

## What we measure

**Load** — [GuideLLM](https://github.com/vllm-project/guidellm) `0.8.1`, driving
`vllm serve` over HTTP. Three scenarios, byte-identical between devices:

| id | profile | data | constraint |
|---|---|---|---|
| `sync` | `synchronous` | 512 prompt / 128 output tokens | 60 requests |
| `conc8` | `concurrent, streams=8` | 512 / 128 | 60 s |
| `sweep` | `sweep` (opt-in) | 512 / 128 | 30 s per point |

`sweep` is not in the default set. Its second strategy probes throughput at
concurrency 16, which needs ~16 x 128 / throughput seconds to finish a single
round; on this host one 512/128 request takes 13 s, so that point completed **zero**
of its 16 requests inside a 30 s window and GuideLLM refused to compile the
benchmark (`No measurement start or end times available`). The rate ladder it then
interpolated ran *down* through 0.06-0.03 req/s — below what `sync` (0.077) and
`conc8` (0.133) already measure directly. The run is kept in
[`results/evidence/failed-sweep-cpu-ref.txt`](results/evidence/failed-sweep-cpu-ref.txt).
Enable it where the payload fits:
`SCENARIOS="sync conc8 sweep" SWEEP_SECONDS=180 harness/run_matrix.sh`.

**Where time goes** — three profilers, each answering a different question:

| tool | answers | granularity | status |
|---|---|---|---|
| `torch.profiler` (CPU / CPU+CUDA) | which vLLM stages and ATen ops own the time | per-op, per-Python-frame | run by the harness |
| `py-spy` | Python-level breakdown without instrumentation | sampling, whole stack | `harness/pyspy_cpu.sh` |
| `perf` / CUDA trace | native kernels under ATen | device kernels | optional, out of scope |

The torch trace is the instrumented source of truth for both legs: it is the only one
that yields the same stage taxonomy on CPU and GPU. `py-spy` is there to catch what
instrumentation distorts (profiler overhead changes the CPU balance).

Both legs run the **same model runner** — v0.33.0 ships two (V1/V2) and would otherwise
pick V2 on the GPU and V1 on the CPU, which changes the Python frames under the profiler.
`harness/env.sh` pins `VLLM_USE_V2_MODEL_RUNNER=0`; see `docs/01-architecture.md`.

Both legs are read through **one stage taxonomy** (`docs/03-stage-taxonomy.md`) so the
comparison chart is not apples-to-oranges.

## Reproduce

Two virtualenvs, on purpose: the server environment is vLLM's (built from source on the
CPU leg) and the load generator is GuideLLM's. Their dependency pins fight each other, and
the load generator is not part of what is being measured.

```bash
# 0. prerequisites
#    CPU leg: headers for the source build (no CPU wheels are published)
#      Fedora/RHEL:      sudo dnf install -y numactl-devel
#      Debian/Ubuntu:    sudo apt-get install -y libnuma-dev
uv venv --python 3.12 .venv          # vLLM
uv venv --python 3.12 .venv-loadgen  # GuideLLM + matplotlib
uv pip install --python .venv-loadgen/bin/python --torch-backend=cpu \
    'guidellm==0.8.1' matplotlib

# 0a. self-test the analysis pipeline with no GPU, no model, no vLLM (seconds)
analysis/demo.sh

# 0b. build the CPU leg from source (~30-45 min, -march=native, host-specific)
harness/build_cpu.sh
#     GPU leg instead installs the same pin as a wheel into .venv, which is why
#     harness/env.sh looks in both places:
#       uv pip install --python .venv/bin/python 'vllm==0.33.0'

# 0c. record what you are measuring with, then prove it
harness/collect_env.sh   > results/env-cpu-$(hostname).txt
harness/probe_isa.sh     > results/isa-cpu-$(hostname).txt   # CPU leg only
DEVICE=cpu VARIANT=int8 harness/check_serve.sh               # boot + one completion + shut down

# 1. serve the model; the device/variant pair is the only switch
DEVICE=cpu VARIANT=int8 harness/serve.sh &

# 2. load (the default set is identical on both legs; `sweep` is opt-in, see R4)
for s in sync conc8; do harness/run_scenario.sh $s cpu-int8; done

# 3. profile one scenario (writes a trace, not a benchmark; see R9 in docs/02)
DEVICE=cpu VARIANT=int8 harness/profile_scenario.sh conc8
#    Low-load alternative for an internals comparison: serial requests, no
#    GuideLLM -> traces/<tag>-profile/. See "Profile-only runs" below.
DEVICE=cpu VARIANT=ref harness/profile_run.sh

# ...or do the whole leg in one go
DEVICE=cpu harness/run_matrix.sh
# ...or one variant/scenario of it, after a partial failure
DEVICE=cpu VARIANT=int8 SCENARIOS=conc8 harness/run_matrix.sh

# 4. analyse, plot, and tabulate
PY=.venv-loadgen/bin/python        # analysis needs matplotlib; vLLM's venv does not have it
"$PY" analysis/analyze_trace.py --trace traces/cpu-int8-conc8/*.json.gz \
       --out results/cpu-int8-conc8-stages.csv
"$PY" analysis/plot_compare.py results/*-stages.csv --out figures/
"$PY" analysis/step_report.py --trace "traces/cpu-int8-conc8/rank0*.json.gz" \
       --fig figures/fig4-step-anatomy.png
"$PY" harness/summarize.py --results results --out results/summary.csv
```

`analysis/step_report.py` is the second question down from `analyze_trace.py`: not "where
did the time go overall" but "what was in this step and what was the scheduler's batch"
(see [`docs/04-request-lifecycle.md`](docs/04-request-lifecycle.md)). It reads the worker's
own `execute_context_*(...)_generation_*(...)` annotations, so it only works on engine-core
trace files (`rank0*`), not on the frontend one.

### Profile-only runs (low load)

Comparing CPU and GPU request processing needs **two traces**, not two benchmark matrices:
`docs/03-stage-taxonomy.md` is read off the engine's own annotations, and the traced window
is capped by engine iterations (R9), not by duration. So the load only has to *produce* those
iterations, and `harness/profile_run.sh` does that with a handful of serial requests instead
of GuideLLM:

```bash
DEVICE=cpu VARIANT=ref harness/profile_run.sh    # -> traces/cpu-ref-profile/rank0*
DEVICE=gpu VARIANT=ref harness/profile_run.sh    # -> traces/gpu-ref-profile/rank0*
```

Each run sends one warmup request, then `PROFILE_REQUESTS` (4) serial requests of
`PROFILE_PROMPT_TOKENS` (512) / `PROFILE_MAX_TOKENS` (128) with `ignore_eos`, under
`max_iterations=20`. Prompt length is measured with the server's own tokenizer and reported
in `results/<tag>-profile.requests.json`, so the prefill size in the trace is a fact rather
than an assumption. Identical prompt text on both legs means identical prefill tokens and
identical decode steps — the traces line up request for request.

GuideLLM is deliberately not used here. Its open-loop concurrent profile holds 8 streams
for a full minute and starts a request-loader process that prefetches thousands of samples;
on the RTX 4060 host that drives sustained maximum load long after the profiler has stopped
recording, and it has taken that machine down. Keep `run_scenario.sh`/`run_matrix.sh` for
throughput numbers, which do need sustained concurrency — and note that a *profiled* run is
eager, so its timings understate GPU throughput anyway (R6).

On the GPU leg the script refuses to start above `GPU_TEMP_MAX` (default 80 °C) and prints
the temperature before and after, because an overheating host is the failure mode that
matters here.

Both legs use the reference variant so the weights are the same and only the device
differs. The dtype still differs by design (CPU `float32`, GPU `bfloat16`): the CPU has no
bf16 hardware, so `dtype_for` gives each device its optimal precision.

### Reading the two traces together

`analysis/compare_runs.py` merges the two per-run CSVs into one op-class table (absolute
ms, share, and the per-op ratio), and `analysis/plot_compare.py` draws the stage,
op-class and projection bars from the same files:

```bash
analysis/compare_runs.py --cpu results/cpu-ref-profile-stages.csv \
                         --gpu results/gpu-ref-profile-stages.csv \
                         --out results/profile-compare.csv
analysis/plot_compare.py results/cpu-ref-profile-stages.csv \
                         results/gpu-ref-profile-stages.csv --out figures/profile
```

The result, with the per-step anatomy and the caveats (different metrics per device,
different dtypes by design, why decode is bandwidth-bound and prefill is not), is in
[`docs/05-cpu-vs-gpu-profile.md`](docs/05-cpu-vs-gpu-profile.md).

### GPU startup troubleshooting

The GPU leg needs a host compiler that the installed CUDA toolkit accepts, and
the failure is easy to misread: vLLM starts, loads the model, compiles, and only
then dies in FlashInfer's JIT step, because FlashInfer builds its sampling kernel
at first use even when vLLM itself came from a wheel.

```
flashinfer.jit: Building JIT module sampling
/usr/local/cuda-13.3/include/crt/host_config.h:137:2: error:
    #error -- unsupported GNU version! gcc versions later than 15 are not supported!
```

CUDA 13.3 accepts GCC ≤ 15; a rolling distribution that already ships GCC 16
(e.g. Fedora 44) therefore has no usable system compiler. Get a GCC 15 without
root and point the build at it:

```bash
conda create -y -p ~/.local/conda-gcc15 --override-channels -c conda-forge \
    gxx_linux-64=15
GCC15=~/.local/conda-gcc15/bin
CC=$GCC15/x86_64-conda-linux-gnu-gcc \
CXX=$GCC15/x86_64-conda-linux-gnu-g++ \
  DEVICE=gpu VARIANT=fp8 harness/check_serve.sh
```

Both variables are required: FlashInfer passes `CC` to `nvcc -ccbin` for `.cu`
files and compiles the C++ binding with `CXX`, so setting only one leaves half
the build on the wrong compiler. Do not use `--allow-unsupported-compiler` for
study runs.

To keep the setting for every later run on that host, put it in the untracked
`harness/env.local.sh`, which `harness/env.sh` sources last:

```bash
GCC15=$HOME/.local/conda-gcc15/bin
export CC=$GCC15/x86_64-conda-linux-gnu-gcc
export CXX=$GCC15/x86_64-conda-linux-gnu-g++
```

Startup also reads as a hang: a first launch spends several minutes downloading
weights and compiling kernels before it opens the HTTP port, so raise the limit
with `SERVE_CHECK_TIMEOUT=1200`. `check_serve.sh` names its log file, and on an
early exit prints the log tail, so a dead server never looks like a timeout.

Profiled runs are single-process (`VLLM_ENABLE_V1_MULTIPROCESSING=0`) and bounded by engine
iterations (`max_iterations=20`), because a multiprocess profile only records the idle
frontend and an unbounded concurrent profile never finishes writing its trace. Throughput
runs keep vLLM's defaults. See [R9](docs/02-methodology.md).

## Results: CPU leg

Intel i7-12800H, 14 threads (physical cores), vLLM v0.33.0 built with `-march=native`,
`--max-num-seqs 16`, 512 prompt / 128 output tokens per request, GuideLLM 0.8.1.
Preemption count is 0 for every scenario (checked around each run; the server logs
contain no preemption events).

| run | requests ok/incomplete | req/s | output tok/s | TTFT mean | ITL mean |
|---|---|---|---|---|---|
| `cpu-ref-sync` (fp32) | 60 / 0 | 0.077 | 9.8 | 1370 ms | 91.6 ms |
| `cpu-int8-sync` (W8A8) | 60 / 0 | 0.150 | 19.2 | 698 ms | 46.9 ms |
| `cpu-ref-conc8` (fp32) | 8 / 8 | 0.133 | 17.1 | 7710 ms | 293.4 ms |
| `cpu-int8-conc8` (W8A8) | 15 / 8 | 0.250 | 32.0 | 4388 ms | 203.7 ms |

INT8 W8A8 is **1.95x** the fp32 generation rate at concurrency 1 and **1.87x** at
concurrency 8. At concurrency 8 the fp32 point costs 7.7 s of TTFT for 17 output
tok/s: on 14 cores the second stream buys almost nothing.

Attributed work in the profiled `conc8` window (20 engine iterations, eager, same
560 projections in both runs):

| | fp32 | INT8 W8A8 | change |
|---|---|---|---|
| all linears | 9559 ms (79.9 %) | 5137 ms (65.7 %) | **-46 %** |
| `linear.gate_up` | 3678 ms | 2041 ms | -44 % |
| `linear.qkv` | 2417 ms | 1279 ms | -47 % |
| `linear.down` | 1759 ms | 788 ms | -55 % |
| `linear.o_proj` | 1263 ms | 587 ms | -54 % |
| `linear.lm_head` | 442 ms (3.7 %) | 442 ms (5.7 %) | **unchanged** |
| attention (`paged`) | 1216 ms (10.2 %) | 1281 ms (16.4 %) | +5 % |
| elementwise | 846 ms (7.1 %) | 928 ms (11.9 %) | +10 % |
| activation | 152 ms (1.3 %) | 162 ms (2.1 %) | +7 % |
| int8 `quant` | - | 121 ms (1.5 %) | - |
| engine scheduling | 2.2 ms | 0.5 ms | - |

The whole INT8 gain comes from the GEMMs. Attention and elementwise work are not
quantized, so their absolute cost is flat and their *share* rises: after
quantization, ~1/3 of the model's attributed work is no longer in a GEMM, and
`lm_head` — which the artifact leaves in fp32 — costs the same 442 ms in both runs
(identical to the millisecond, which is a useful cross-check that the two traces
are being measured the same way).

Figures: [`figures/fig1-stages.png`](figures/fig1-stages.png),
[`fig2-op-classes.png`](figures/fig2-op-classes.png),
[`fig3-linear.png`](figures/fig3-linear.png),
[`fig4-step-anatomy.png`](figures/fig4-step-anatomy.png) (per-layer anatomy of one
prefill and one decode step). Numbers:
[`results/summary.csv`](results/summary.csv), `results/*-stages.csv`.

For what a *single step* is made of — the scheduler's own batch decision, the phase split,
the per-layer anatomy, and why the scenario ITL is larger than any decoded step — see
[`docs/04-request-lifecycle.md`](docs/04-request-lifecycle.md).

GPU leg: not yet run (no GPU on the CPU-side host). The command sequence is the same
with `DEVICE=gpu`.

## Status

- [x] Harness, stage taxonomy, analyzer, plot scaffolding
- [x] CPU extension built from source with native ISA flags, verified end to end
      (fp32 and INT8 W8A8 both produce sane text; dispatched ISA and oneDNN kernel recorded)
- [x] CPU leg: reference (fp32) + INT8 W8A8 numbers
- [ ] GPU leg: reference (bf16) + FP8 W8A8
- [ ] Comparison figures

Numbers land in `results/` (JSON/CSV only — raw traces are gitignored, see
`.gitignore`). Everything here is measurement code, not a vLLM contribution.

## Layout

```
docs/       architecture primer, methodology, stage taxonomy, step-by-step anatomy
harness/    environment capture, server launch, GuideLLM scenarios, profiling
analysis/   trace -> normalized stages + comparison plots
offline/    earlier offline `LLM(...)` scripts (superseded by the server harness,
            kept because they are handy for single-process kernel-benchmark work)
data/       prompt padding used to control prefill length
results/    committed metrics (JSON/CSV), never traces
figures/    committed charts
SESSION-TRANSCRIPT.md   the conversation that produced all of the above, readable
SESSION-EXPORT.json     ...and the same conversation in raw `opencode session export` form
```
