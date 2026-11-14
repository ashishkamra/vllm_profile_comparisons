# vLLM CPU vs GPU: where the time actually goes

A reproducible study of the **same model, same serving scenarios, two very different
accelerators**, with matched load generation (GuideLLM) and matched profiling, so the
latency numbers and the profiler traces can be read against each other.

The point is not "GPU is faster". It is: *the two devices spend their time in
different places, in ways you can see in a trace and predict from the hardware.*

| | CPU leg | GPU leg |
|---|---|---|
| Hardware | Intel i7-12800H (Alder Lake, AVX2 + AVX-VNNI, **no AVX-512, no AMX**) | NVIDIA RTX 4060 8 GB (Ada, sm_89) |
| vLLM | `7833dce847` (`v0.34.0rc0`), built from source with `VLLM_TARGET_DEVICE=cpu` | same commit, precompiled wheel |
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
| `conc8` | `concurrent, max_concurrency=8` | 512 / 128 | 60 s |
| `sweep` | `sweep` | 512 / 128 | 30 s per point |

**Where time goes** — three profilers, each answering a different question:

| tool | answers | granularity |
|---|---|---|
| `torch.profiler` (CPU/CPU+CUDA) | which vLLM stages and ATen ops own the time | per-op, per-Python-frame |
| `py-spy` | Python-level breakdown without instrumentation | sampling, whole stack |
| `perf` (CPU) / `ncu`-free CUDA trace (GPU) | native kernels under ATen | device kernels |

Both legs are read through **one stage taxonomy** (`docs/03-stage-taxonomy.md`) so the
comparison chart is not apples-to-oranges.

## Reproduce

```bash
# 0. prerequisites
#    CPU leg: system headers for the source build (no CPU wheels exist)
#      Fedora/RHEL:      sudo dnf install -y numactl-devel
#      Debian/Ubuntu:    sudo apt-get install -y libnuma-dev
#    both legs:
uv venv --python 3.12 .venv && source .venv/bin/activate
uv pip install 'guidellm==0.8.1' matplotlib

# 0a. self-test the analysis pipeline with no GPU, no model, no vLLM (seconds)
analysis/demo.sh

# 0b. environment (record it — the report embeds this)
harness/collect_env.sh > results/env-$(hostname).txt

# 1. serve the model; the device/variant pair is the only switch
DEVICE=cpu VARIANT=int8 harness/serve.sh &

# 2. load (three scenarios, identical on both legs)
for s in sync conc8 sweep; do harness/run_scenario.sh $s cpu-int8; done

# 3. profile one scenario (writes a trace, not a benchmark)
DEVICE=cpu VARIANT=int8 harness/profile_scenario.sh conc8

# ...or do the whole leg in one go
DEVICE=cpu harness/run_matrix.sh

# 4. analyse + plot
python analysis/analyze_trace.py --trace traces/cpu-int8-conc8/*.json.gz \
       --out results/cpu-int8-conc8-stages.csv
python analysis/plot_compare.py results/*.csv --out figures/
```

## Status

- [x] Harness, stage taxonomy, analyzer, plot scaffolding
- [ ] CPU leg: reference (fp32) + INT8 W8A8
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
