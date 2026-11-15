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

**The CPU leg emits no `aten::linear` at all.** vLLM dispatches the projection straight to
its own kernels, and the recorded name is the dispatched op (`_C::onednn_mm`,
`_C::onednn_scaled_mm`, `_C::sgl_gemm`, `cutlass_scaled_mm`, ...). Two dims conventions
therefore exist in the same study, and the analyzer picks one from the op name rather than
guessing:

| family | `Input Dims` layout | (K, N) |
|---|---|---|
| `aten::linear` | `input (..., K)`, `weight (N, K)` | `dims[0][-1]`, `dims[1][0]` |
| `aten::mm`, `aten::matmul`, `aten::addmm` | `mat1 (..., M, K)`, `mat2 (K, N)` (`addmm` puts the 1-D bias first) | `dims[-2][-1]`, `dims[-1][-1]` |
| vLLM custom kernels (`onednn*`, `sgl_*`, `cutlass_*`) | `out (..., N)`, `input (..., K)` | `dims[1][-1]`, `dims[0][-1]` |

The `mm` row is not cosmetic: parsing it like a linear weight (`dims[1][0]`) gives `(K, K)`,
so every projection comes out square — plausible-looking (`linear[1024x1024]`) and wrong.
That is what a GPU leg looked like before the rule was fixed; the case is pinned by
`test_aten_mm_uses_the_mat2_layout`.

Real int8 example from a `rank0` trace: a fused QKV GEMM records as
`_C::onednn_scaled_mm` with
`Input Dims = [[64, 4096], [64, 1024], [64, 1], [], [1, 4096], [], []]`, so
`out = [64, 4096] -> N = 4096` and `input = [64, 1024] -> K = 1024`, hence `linear.qkv`.
Getting this convention backwards silently relabels every projection, which is why it is
pinned by tests in `analysis/test_stages.py` using dims copied from a real trace.

`lm_head` arrives as `_C::onednn_mm` in fp32 (the head is not quantized in the W8A8
artifact), so the vocab-size rule applies to the float path as well.

Op classes that do not come from shapes:

| op class | match |
|---|---|
| `attention.paged` | kernel/op name contains `cpu_attention_with_kv_cache`, `paged_attention`, `flash_fwd`, `_efficient_attention`, `triton_attention` |
| `attention.kv_write` | `cpu_attn_reshape_and_cache`, `reshape_and_cache`, `cache_kernels` |
| `norm` | `aten::rms_norm`, `aten::layer_norm`, `*norm*` |

`norm` only appears when the kernel exists as an op. On x86 CPU vLLM decomposes RMSNorm
into `aten::pow/mul/mean/div_`, so name-based classification lands it in `elementwise`;
`analysis/step_report.py` attributes ops by their enclosing `nn.Module` frame instead and
recovers a `norm` row. See `docs/04-request-lifecycle.md`.
| `rope` | `rotary`, `aten::rope`, `apply_rotary` |
| `activation` | `aten::silu`, `aten::gelu`, `aten::sigmoid`, `silu_and_mul` |
| `quant` | `dynamic_scaled_int8_quant`, `scaled_int8_quant`, `quantize_per_tensor`, `fp8_quant`, `_C::quant` |
| `elementwise` | remaining `aten::` pointwise ops (`mul`, `add`, `sub`, `div`, `to`, `copy_`, ...) |
| `sampler` | `argmax`, `softmax`, `topk`, `gumbel`, `multinomial` |
| `python` | no ATen op under the frame: real Python overhead |

`quant` is listed separately because it is the tax the deployment artifact pays for
quantized weights: on the CPU INT8 leg it is ~1 % of attributed time over 2240 calls, and
it would be invisible if it were folded into `elementwise`. It has no GPU-FP8 counterpart
of the same name, so compare it as "quantization overhead", not op-for-op.

## What counts as attributed time

Only **work** is attributed:

| trace | attributed events |
|---|---|
| CPU | `cpu_op` (ATen and vLLM custom ops) and `user_annotation` |
| GPU | device `kernel`, `gpu_memcpy`, `gpu_memset`, correlated back to the launching op |

Bare `python_function` *self* time is deliberately excluded. In a serving trace it is
dominated by asyncio and queue waits: on an idle CPU int8 profile it is about 51 s of
"self time" against 2.2 s of actual op time, so including it would turn every chart into a
chart of waiting. The analyzer prints that total next to the attributed total so the
omission stays visible, and `python` survives as an op class only for ops that have *no*
ATen child, which is real interpreter overhead inside `model.forward` rather than idle time.

A trace that contains no attributed events at all is skipped with an explanation: that
shape means a frontend-only trace from a multiprocess profile run (see R9).

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
