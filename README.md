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
| `sweep` | `sweep` | 512 / 128 | 30 s per point |

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

# 0c. record what you are measuring with, then prove it
harness/collect_env.sh   > results/env-$(hostname).txt
harness/probe_isa.sh     > results/isa-$(hostname).txt   # CPU leg only
DEVICE=cpu VARIANT=int8 harness/check_serve.sh           # boot + one completion + shut down

# 1. serve the model; the device/variant pair is the only switch
DEVICE=cpu VARIANT=int8 harness/serve.sh &

# 2. load (three scenarios, identical on both legs)
for s in sync conc8 sweep; do harness/run_scenario.sh $s cpu-int8; done

# 3. profile one scenario (writes a trace, not a benchmark; see R9 in docs/02)
DEVICE=cpu VARIANT=int8 harness/profile_scenario.sh conc8

# ...or do the whole leg in one go
DEVICE=cpu harness/run_matrix.sh

# 4. analyse + plot
"$PY" analysis/analyze_trace.py --trace traces/cpu-int8-conc8/*.json.gz \
       --out results/cpu-int8-conc8-stages.csv
"$PY" analysis/plot_compare.py results/*-stages.csv --out figures/
```

Profiled runs are single-process (`VLLM_ENABLE_V1_MULTIPROCESSING=0`) and bounded by engine
iterations (`max_iterations=20`), because a multiprocess profile only records the idle
frontend and an unbounded concurrent profile never finishes writing its trace. Throughput
runs keep vLLM's defaults. See [R9](docs/02-methodology.md).

## Status

- [x] Harness, stage taxonomy, analyzer, plot scaffolding
- [x] CPU extension built from source with native ISA flags, verified end to end
      (fp32 and INT8 W8A8 both produce sane text; dispatched ISA and oneDNN kernel recorded)
- [ ] CPU leg: reference (fp32) + INT8 W8A8 numbers
- [ ] GPU leg: reference (bf16) + FP8 W8A8
- [ ] Comparison figures

Numbers land in `results/` (JSON/CSV only — raw traces are gitignored, see
`.gitignore`). Everything here is measurement code, not a vLLM contribution.

## Layout

```
docs/       architecture primer, methodology, stage taxonomy
harness/    environment capture, server launch, GuideLLM scenarios, profiling
analysis/   trace -> normalized stages + comparison plots
offline/    earlier offline `LLM(...)` scripts (superseded by the server harness,
            kept because they are handy for single-process kernel-benchmark work)
data/       prompt padding used to control prefill length
results/    committed metrics (JSON/CSV), never traces
figures/    committed charts
```
