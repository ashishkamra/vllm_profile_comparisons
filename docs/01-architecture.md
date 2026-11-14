# 1. The shape of a vLLM request

Reading a profile requires knowing which Python frame is supposed to be doing work.
This is the map, pinned to `7833dce847` (`v0.34.0rc0`). File paths are relative to
the vLLM repo root.

## One iteration = one "step"

`vllm serve` runs two processes (offline `LLM` keeps both in one):

```
frontend process                         engine-core process
────────────────                         ────────────────────
tokenizer / detokenizer                  EngineCore.step()
request admission                        ├─ Scheduler.schedule()
AsyncLLM output handling                 ├─ KVCacheManager block alloc/free
                                         ├─ Worker.execute_model()   ← the math
                                         └─ Scheduler.update_from_output()
```

### Frontend
`vllm/v1/engine/llm_engine.py`, `vllm/v1/engine/async_llm.py`.
Cheap per step, but it owns tokenization, detokenization, stop-string matching, and
the HTTP boundary. On a fast accelerator this is where head-of-line overhead shows up;
it is also where GuideLLM's TTFT is partly spent.

### Engine core — `vllm/v1/engine/core.py:703 EngineCore.step()`

| stage | code | what it does |
|---|---|---|
| schedule | `v1/core/sched/scheduler.py:685` | pick requests, split prefill tokens (chunked prefill), decide decode tokens, allocate KV blocks |
| KV bookkeeping | `v1/core/kv_cache_manager.py` | block tables, prefix-cache hits, frees on finish |
| execute | worker → model runner | the math (below) |
| postprocess | `scheduler.py:2245 update_from_output` | append sampled tokens, stop checks, free blocks |

`schedule()` is pure Python. On a GPU it is nearly free but non-zero per step; on CPU
with a tiny model it can be a measurable share. That asymmetry is one of the things the
comparison is designed to expose.

## The worker: one shared runner, per-device kernels

Since the Model Runner V2 refactor, there is a single runner implementation. Note that
`vllm/v1/worker/cpu/model_runner.py` is 16 lines:

```python
class CPUModelRunner(GPUModelRunner):
    def warming_up_model(self) -> None: ...
```

So even on CPU you will see `vllm/v1/worker/gpu/model_runner.py` in stack traces. Do not
read that as "running GPU code" — it is the shared runner; the platform injects its own
linear/attention kernels.

`execute_model()` (`v1/worker/gpu/model_runner.py:1821`) does:

1. **prepare_inputs** — slot mapping (where each token's KV gets written), positions,
   block tables, attention metadata. Python + C++/CUDA helpers.
2. **forward** — `embedding → N × decoder layer → final norm → lm_head`
3. **sample** — `v1/sample/` (logits processors, penalties, structured output) and, on
   CPU, `v1/worker/cpu/sampler.py:543 CPUSampler`

A decoder layer for Qwen3 is the usual modern stack, and each stage is a separate
Python call — which is what makes per-stage attribution possible from stack traces:

```
RMSNorm → qkv_proj (fused QKV linear) → RoPE → paged attention → o_proj
        → RMSNorm → gate_up_proj (fused) → SiLU/SwiGLU → down_proj
```

## The device-specific layer (this is the actual difference)

Everything above is identical on both machines. What differs is the kernel that each
Python call dispatches to.

**CPU** (`vllm/platforms/cpu.py`)
- linear → `F.linear` → **oneDNN** (`libtorch_cpu.so`) with AVX2/AVX-VNNI; INT8 W8A8
  goes through `vllm/model_executor/kernels/linear/scaled_mm/cpu.py` →
  `CPUInt8ScaledMMLinearKernel`, which picks the AMX/SGL kernel when AMX exists and
  falls back to **oneDNN** otherwise.
- attention → `vllm/v1/attention/backends/cpu_attn.py` → the `_cpu_ops` C++ extension
  (`csrc/cpu/cpu_attn.cpp`): `cpu_attn_reshape_and_cache`,
  `cpu_attention_with_kv_cache`, with ISA-specialised implementations (`cpu_attn_amx.hpp`,
  `cpu_attn_vec.hpp`, …) selected at build time.
- no CUDA graphs, no async streams: one host thread does everything, so *host time is
  wall time*.

**GPU**
- linear → tensor-core GEMMs (`cutlass_scaled_mm` for FP8, `torch.mm`/cuBLAS for bf16).
- attention → FlashAttention / FlashInfer / Triton paged attention.
- launch-heavy and asynchronous: `execute_model` returns long before the kernels finish,
  CUDA graphs replace launch sequences with a single replay. So a GPU trace has
  *host spans*, *device kernels*, and *graph replays* — and total wall time is device
  time when the queue is deep. See `docs/02-methodology.md` for how the analyzer treats
  that.

## Model-specific thing worth knowing (Qwen3-0.6B)

`tie_word_embeddings: true` and a 151936-token vocabulary. Decode produces one token per
sequence but must still project a hidden state to the full vocabulary through `lm_head`
each step: 151936 × 1024. For a 0.6B model that matmul is a *large* fraction of decode
time — on both devices, for different reasons (GPU: it is a skinny GEMM that cannot fill
the machine; CPU: it is just a lot of FLOPs). Expect `lm_head` to be visible in the
profile. If it is not, be suspicious of the trace.
