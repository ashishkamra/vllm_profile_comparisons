# 1. The shape of a vLLM request

Reading a profile requires knowing which Python frame is supposed to be doing work.
This is the map, pinned to **v0.33.0** (`4ba787ec99`). File paths are relative to the
vLLM repo root — they differ from later versions, which is why the pin matters.

## One iteration = one "step"

`vllm serve` runs two processes (offline `LLM` keeps both in one):

```
frontend process                         engine-core process
────────────────                         ────────────────────
tokenizer / detokenizer                  EngineCore.step()          core.py:667
request admission                        ├─ Scheduler.schedule()    scheduler.py:699
AsyncLLM output handling                 ├─ KVCacheManager block alloc/free
                                         ├─ Worker.execute_model()  ← the math
                                         └─ Scheduler.update_from_output()
```

### Frontend
`vllm/v1/engine/llm_engine.py`, `vllm/v1/engine/async_llm.py`.
Cheap per step, but it owns tokenization, detokenization, stop-string matching, and
the HTTP boundary. On a fast accelerator this is where head-of-line overhead shows up;
it is also where GuideLLM's TTFT is partly spent.

### Engine core

| stage | code | what it does |
|---|---|---|
| schedule | `v1/core/sched/scheduler.py:699` | pick requests, split prefill tokens (chunked prefill), decide decode tokens, allocate KV blocks |
| KV bookkeeping | `v1/core/kv_cache_manager.py` | block tables, prefix-cache hits, frees on finish |
| execute | worker → model runner | the math (below) |
| postprocess | `scheduler.py update_from_output` | append sampled tokens, stop checks, free blocks |

`schedule()` is pure Python. On a GPU it is nearly free but non-zero per step; on CPU
with a tiny model it can be a measurable share. That asymmetry is one of the things the
comparison is designed to expose.

## The worker: two runners in v0.33, pin V1

v0.33.0 ships **two** model runners. This is the single most important version detail for
this study, because the two have different Python frame structures:

| runner | GPU class | CPU subclass | needs Triton |
|---|---|---|---|
| V1 (`VLLM_USE_V2_MODEL_RUNNER=0`) | `v1/worker/gpu_model_runner.py` | `v1/worker/cpu_model_runner.py:23` | no |
| V2 (`VLLM_USE_V2_MODEL_RUNNER=1`) | `v1/worker/gpu/model_runner.py` | `v1/worker/cpu/model_runner.py` | yes |

Triton is disabled on a CPU-only host (`Triton is installed but 0 active driver(s) found`
→ `HAS_TRITON=False`), so **auto-selection gives V2 on the GPU and V1 on the CPU**. Left
alone, the two legs would be profiled through different code and the stage comparison
would be meaningless for reasons unrelated to hardware.

`harness/env.sh` therefore pins `VLLM_USE_V2_MODEL_RUNNER=0` on both legs: V1 is the only
runner both devices can use at this version. The taxonomy in `analysis/stages.py`
understands both, so a deliberate V2 run can still be analysed — it just cannot be mixed
with a V1 run in the same chart.

In V1, `GPUModelRunner.execute_model()` is `v1/worker/gpu_model_runner.py:3906` and:

1. **prepare** — `_prepare_inputs` (`:1924`): slot mapping (where each token's KV gets
   written), positions, block tables, attention metadata. Python + C++/CUDA helpers.
2. **forward** — `embedding → N × decoder layer → final norm → lm_head`
3. **sample** — `vllm/v1/sample/sampler.py` (constructed at `:563`): logits processors,
   penalties, structured output, then argmax/gumbel

The CPU runner is a thin subclass that makes accelerator APIs no-ops and adds a warmup:

```python
class CPUModelRunner(GPUModelRunner):   # v1/worker/cpu_model_runner.py:23
```

So you will see `gpu_model_runner.py` in a *CPU* trace. That is expected — it is the
shared runner, and the platform injects its own linear/attention kernels.

A decoder layer for Qwen3 is the usual modern stack, and each stage is a separate
Python call — which is what makes per-stage attribution possible from stack traces:

```
RMSNorm → qkv_proj (fused QKV linear) → q/k norm → RoPE → paged attention → o_proj
        → RMSNorm → gate_up_proj (fused) → SiLU/SwiGLU → down_proj
```

## The device-specific layer (this is the actual difference)

Everything above is identical on both machines. What differs is the kernel that each
Python call dispatches to.

**CPU** (`vllm/platforms/cpu.py`)
- linear → `F.linear` → **oneDNN** (`libtorch_cpu.so`) with AVX2/AVX-VNNI; INT8 W8A8
  goes through `vllm/model_executor/kernels/linear/scaled_mm/cpu.py` →
  `CPUInt8ScaledMMLinearKernel`, which picks the AMX/SGL kernel only when
  `torch.cpu._is_amx_tile_supported()` and otherwise falls back to **oneDNN**.
- attention → `vllm/v1/attention/backends/cpu_attn.py` → the `_cpu_ops` C++ extension
  (`csrc/cpu/cpu_attn.cpp`): `cpu_attn_reshape_and_cache`,
  `cpu_attention_with_kv_cache`, `cpu_attn_get_scheduler_metadata`, with ISA-specialised
  implementations (`cpu_attn_amx.hpp`, `cpu_attn_vec.hpp`, …) chosen at build time.
- no CUDA graphs, no async streams: host time *is* wall time.

**ISA note for this study.** The CPU extension ships as several `.so` variants
(`_C`, `_C_AVX2`, `_C_AVX512`, `_C_AVX512_AMX`) and dispatches at import time. The build in
`harness/build_cpu.sh` adds `-march=native` (`-march=alderlake` here: AVX2, AVX-VNNI,
GFNI, VAES, BMI2, FMA, F16C; no AVX-512, no AMX), so the variants this host can actually
execute are compiled for it. oneDNN does its own runtime ISA dispatch regardless, which
is where the INT8 GEMM actually lands (VNNI, not AMX).

**GPU**
- linear → tensor-core GEMMs (`cutlass_scaled_mm` for FP8, cuBLAS for bf16).
- attention → FlashAttention / FlashInfer / Triton paged attention.
- launch-heavy and asynchronous: `execute_model` returns long before the kernels finish,
  CUDA graphs replace launch sequences with a single replay. So a GPU trace has
  *host spans*, *device kernels*, and *graph replays* — and total wall time is device
  time when the queue is deep. See `docs/02-methodology.md` for how the analyzer treats
  that.

## Model-specific thing worth knowing (Qwen3-0.6B)

`hidden_size=1024`, 16 Q heads / 8 KV heads, `head_dim=128`, `intermediate_size=3072`,
28 layers, `tie_word_embeddings: true`, `vocab_size=151936`.

Decode produces one token per sequence but must still project a hidden state to the full
vocabulary through `lm_head` each step: 151936 × 1024. For a 0.6B model that matmul is a
*large* fraction of decode time — on both devices, for different reasons (GPU: it is a
skinny GEMM that cannot fill the machine; CPU: it is a 622 MB fp32 weight read per step, so
it is bandwidth-bound and costs the same for 8 rows as for 512 — measured in
`docs/04-request-lifecycle.md`). Expect `linear.lm_head` to be visible in the profile. If
it is not, be suspicious of the trace.

The fused projections also give the shape key the analyzer uses to name sub-stages
without patching the model:

| (K, N) | layer |
|---|---|
| (1024, 4096) | `qkv_proj` — 2048 (Q) + 1024 (K) + 1024 (V) |
| (2048, 1024) | `o_proj` |
| (1024, 6144) | `gate_up_proj` |
| (3072, 1024) | `down_proj` |
| (1024, 151936) | `lm_head` |
