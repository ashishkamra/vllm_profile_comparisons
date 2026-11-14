# 3. Stage taxonomy — the thing both legs are aggregated into

A CPU trace and a CUDA trace do not share structure. If you aggregate each with its own
natural grouping you get two charts you cannot put next to each other. So both legs are
mapped into **one fixed vocabulary** before anything is plotted.

Two orthogonal axes. Every row of `results/*.csv` carries both.

## Axis A — engine stage (from Python frames)

Attribution comes from `torch.profiler`'s `python_function` events, which exist on both
devices because the *same Python code* runs on both. The nearest enclosing frame decides
the stage. `with_stack=True` is required.

| stage | frame match (substring of the file in the frame name) |
|---|---|
| `frontend.tokenize` | `v1/engine/`, `entrypoints/`, `tokenizers`, `transformers` tokenizer frames |
| `engine.schedule` | `v1/core/sched/scheduler.py` (`schedule`) |
| `engine.kv_cache` | `v1/core/kv_cache_manager.py`, `v1/core/block_pool.py`, `v1/core/kv_cache_utils.py` |
| `engine.postprocess` | `v1/core/sched/scheduler.py` (`update_from_output`, `_free_request`) |
| `runner.prepare_inputs` | any model-runner file, function matching `prepare_input` |
| `runner.execute_model` | any model-runner file, function matching `execute_model` |
| `runner.glue` | any other frame in a model-runner file |
| `model.forward` | `vllm/model_executor/models/`, `vllm/model_executor/layers/` |
| `model.lm_head` | shape rule, see Axis B (`N = vocab_size`) |
| `sample` | `v1/worker/*/sample/`, `v1/worker/cpu/sampler.py`, `v1/sample/ops/` |
| `frontend.other` | `v1/engine/llm_engine.py`, `v1/engine/async_llm.py`, `v1/engine/output_processor.py` |
| `frontend.detokenize` | `v1/engine/detokenizer.py`, `tokenizer.decode` |
| `other` | anything unmatched — always reported, never silently dropped |

`other` is a health metric. If it exceeds ~10 % the taxonomy is wrong for this commit and
must be fixed before drawing conclusions.

## Axis B — op class (from ATen op names and tensor shapes)

Engine stage tells you *which component*; op class tells you *what kind of work*.
Sub-stage split inside `model.forward` is derived from **shapes**, not from patching the
model, because `layers/linear.py` is one frame used by every projection:

`record_shapes=True` gives `aten::linear` an `Input Dims` argument `[[..., K], [N, K]]`.
For Qwen3-0.6B (hidden 1024, 16 Q heads / 8 KV heads, head_dim 128, inter 3072,
vocab 151936):

| (K, N) | op class | layer |
|---|---|---|
| (1024, 4096) | `linear.qkv` | fused Q/K/V (2048 + 1024 + 1024) |
| (2048, 1024) | `linear.o_proj` | attention output projection |
| (1024, 6144) | `linear.gate_up` | fused gate+up |
| (3072, 1024) | `linear.down` | down projection |
| (1024, 151936) | `linear.lm_head` | vocabulary projection (also promotes the stage to `model.lm_head`) |

Op classes that do not come from shapes:

| op class | match |
|---|---|
| `attention.paged` | kernel/op name contains `cpu_attention_with_kv_cache`, `paged_attention`, `flash_fwd`, `_efficient_attention`, `triton_attention` |
| `attention.kv_write` | `cpu_attn_reshape_and_cache`, `reshape_and_cache`, `cache_kernels` |
| `norm` | `aten::rms_norm`, `aten::layer_norm`, `*norm*` |
| `rope` | `rotary`, `aten::rope`, `apply_rotary` |
| `activation` | `aten::silu`, `aten::gelu`, `aten::sigmoid`, `silu_and_mul` |
| `elementwise` | remaining `aten::` pointwise ops (`mul`, `add`, `sub`, `div`, `to`, `copy_`, …) |
| `sampler` | `argmax`, `softmax`, `topk`, `gumbel`, `multinomial` |
| `python` | no ATen op under the frame: real Python overhead |

## Which time column

| trace kind | column | meaning |
|---|---|---|
| CPU | `self_ms` | plain self time; host time is wall time |
| GPU | `device_ms` | kernel durations correlated to the launching CPU op |
| GPU | `host_ms` | CPU-side span time (launch overhead, Python) |

`self_ms` is what the plots use for CPU. GPU plots use `device_ms` for "where the FLOPs
went" and `host_ms` for "where the Python went"; the two are never added together.
See `docs/02-methodology.md#async-device-time`.

## Rules for adding a stage

1. It must be derivable identically on CPU and GPU.
2. It must not overlap another stage (single-homed).
3. Unmatched time goes to `other`, not to the nearest plausible stage.
4. The mapping lives in `analysis/stages.py` and is unit-testable: `analysis/test_stages.py`
   feeds synthetic frames and asserts the classification.
