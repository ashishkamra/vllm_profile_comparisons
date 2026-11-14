# SPDX-License-Identifier: Apache-2.0
"""Stage taxonomy: map trace events to (engine stage, op class).

The taxonomy is defined in docs/03-stage-taxonomy.md. Both the CPU leg and the GPU
leg are aggregated through this module so the results are directly comparable.

Pure functions, no I/O: see test_stages.py.
"""

from __future__ import annotations

import re
from typing import NamedTuple

# ---------------------------------------------------------------------------
# Axis A: engine stage, from the Python frame that encloses the work.
# Order matters: first match wins, so keep the specific rules above the general.
# ---------------------------------------------------------------------------
FRAME_RULES: list[tuple[str, tuple[str, ...]]] = [
    # frontend
    ("frontend.detokenize", ("v1/engine/detokenizer.py", "tokenizer.decode")),
    ("frontend.tokenize", (
        "v1/engine/input_processor.py",
        "entrypoints/llm.py",
        "tokenizer.encode",
    )),
    ("frontend.other", (
        "v1/engine/llm_engine.py",
        "v1/engine/async_llm.py",
        "v1/engine/output_processor.py",
        "entrypoints/openai/",
    )),
    # engine core
    ("engine.schedule", ("v1/core/sched/scheduler.py",)),
    ("engine.kv_cache", (
        "v1/core/kv_cache_manager.py",
        "v1/core/block_pool.py",
        "v1/core/kv_cache_utils.py",
        "v1/core/kv_cache_coordinator.py",
    )),
    # sampling (shared sampler lives under v1/sample at v0.33, v1/worker/*/sample later)
    ("sample", (
        "v1/worker/gpu/sample/",
        "v1/worker/cpu/sampler.py",
        "v1/sample/",
        "layers/sampler.py",
    )),
    # model
    ("model.forward", (
        "model_executor/models/",
        "model_executor/layers/",
        "model_executor/kernels/",
    )),
]

# vLLM v0.33.0 ships *two* model runners and picks one by config/env:
#   V1  vllm/v1/worker/gpu_model_runner.py  (+ cpu_model_runner.py subclass)
#   V2  vllm/v1/worker/gpu/model_runner.py  (+ cpu/model_runner.py subclass)
# Both are classified the same way so a run is analysable whichever is active.
RUNNER_FILES = (
    "v1/worker/gpu/model_runner.py",
    "v1/worker/gpu_model_runner.py",
    "v1/worker/cpu_model_runner.py",
    "v1/worker/cpu/model_runner.py",
)

# Frames that live in a file that also hosts scheduler work (update_from_output etc.)
POSTPROCESS_MARKERS = ("update_from_output", "_free_request", "_update_request_with_output")
POSTPROCESS_FUNCS = ("_update_states", "update_states", "finish_requests", "_free_request")

# ---------------------------------------------------------------------------
# Axis B: op class, from ATen op name and (optionally) tensor shapes.
# ---------------------------------------------------------------------------
# Every GEMM spelling the two legs actually emit. The CPU leg does not use
# aten::linear at all: vLLM dispatches straight to oneDNN (_C::onednn_mm,
# _C::onednn_scaled_mm) or to the sgl/cutlass kernels.
LINEAR_MARKERS = (
    "aten::linear",
    "aten::mm",
    "aten::addmm",
    "aten::matmul",
    "onednn_mm",
    "onednn_scaled_mm",
    "onednn_qconv",
    "sgl_gemm",
    "cutlass_scaled_mm",
    "gemm",
    # cuBLAS's matrix-vector kernel. At M=1 (decode) every projection is a GEMV,
    # and the name contains no "gemm" -- so without this the largest slice of
    # decode device time is classed "python" (43.7 of 139.3 ms on a 0.6B trace).
    "gemv",
)

QUANT_MARKERS = (
    "dynamic_scaled_int8_quant",
    "scaled_int8_quant",
    "quantize_per_tensor",
    "fp8_quant",
    "_C::quant",
)

ATTENTION_MARKERS = (
    "cpu_attention_with_kv_cache",
    "paged_attention",
    "flash_fwd",
    "_efficient_attention",
    "triton_attention",
    "sdpa",
    "scaled_dot_product",
)
KV_WRITE_MARKERS = ("reshape_and_cache", "cache_kernels", "concat_and_cache")

# Qwen3-0.6B linear shapes -> sub-stage label. Unknown shapes fall back to
# "linear[KxN]" and show up as such in the report (never silently merged).
LINEAR_SHAPES: dict[tuple[int, int], str] = {
    # Qwen3-0.6B: hidden 1024, 16 Q heads / 8 KV heads, head_dim 128,
    # intermediate 3072. Fused QKV output = 2048 (Q) + 1024 (K) + 1024 (V).
    (1024, 4096): "linear.qkv",
    (2048, 1024): "linear.o_proj",
    (1024, 6144): "linear.gate_up",
    (3072, 1024): "linear.down",
}


class Classified(NamedTuple):
    stage: str
    op_class: str


FRAME_RE = re.compile(r"^(?P<file>.*?\.py)(?:\((?P<line>\d+)\))?:? ?(?P<func>[\w.<>_]+)?$")


def parse_frame(name: str) -> tuple[str, str]:
    """Split a kineto python_function event name into (file, function)."""
    m = FRAME_RE.match(name)
    if not m:
        return name, ""
    return (m.group("file") or name), (m.group("func") or "")


def stage_for_frame(file: str, func: str) -> str:
    """Axis A for a single frame. Returns 'other' when nothing matches."""
    if "sched/scheduler.py" in file and func in POSTPROCESS_MARKERS:
        return "engine.postprocess"

    if any(r in file for r in RUNNER_FILES):
        if "prepare_input" in func:
            return "runner.prepare_inputs"
        if "execute_model" in func:
            return "runner.execute_model"
        if func in POSTPROCESS_FUNCS:
            return "engine.postprocess"
        return "runner.glue"

    haystack = f"{file} {func}"
    for stage, markers in FRAME_RULES:
        if any(m in haystack for m in markers):
            return stage
    return "other"


def _dims_of(event_args: dict) -> list[list[int]] | None:
    dims = event_args.get("Input Dims")
    if not dims:
        return None
    return dims


def linear_shape(name: str, event_args: dict) -> tuple[int, int] | None:
    """(K, N) for a GEMM event, else None.

    Three dims layouts exist in practice, and getting one wrong silently
    mislabels projections rather than failing:

      aten::linear   Input Dims = [input (..., K), weight (N, K)]
      aten::mm / matmul / addmm
                     Input Dims = [..., mat1 (M, K), mat2 (K, N)]
                     (addmm additionally puts the 1-D bias first)
      vLLM custom    Input Dims = [out (..., N), input (..., K), ...]
    e.g. _C::onednn_mm / _C::onednn_scaled_mm, whose first arg is the output.

    All with record_shapes=True. The convention is selected from the op name,
    never guessed.
    """
    dims = _dims_of(event_args)
    if not dims or len(dims) < 2:
        return None
    try:
        if name.startswith("aten::linear"):
            a, b = dims[0], dims[1]
            if not a or not b:
                return None
            return int(a[-1]), int(b[0])
        if name.startswith(("aten::mm", "aten::matmul", "aten::addmm")):
            # The last operand is the (K, N) matrix; its last dim is N.
            a, b = dims[-2], dims[-1]
            if not a or not b:
                return None
            return int(a[-1]), int(b[-1])
        a, b = dims[0], dims[1]
        if not a or not b:
            return None
        return int(b[-1]), int(a[-1])
    except (TypeError, IndexError, ValueError):
        return None


def op_class_for(name: str, event_args: dict | None = None, *, vocab_size: int | None = None) -> str:
    """Axis B for one event."""
    event_args = event_args or {}

    # Device kernels and CPU attention both arrive here by name.
    for marker in ATTENTION_MARKERS:
        if marker in name:
            return "attention.paged"
    for marker in KV_WRITE_MARKERS:
        if marker in name:
            return "attention.kv_write"

    if any(m in name for m in LINEAR_MARKERS):
        shape = linear_shape(name, event_args)
        if shape is not None:
            k, n = shape
            if vocab_size and n >= int(0.9 * vocab_size):
                return "linear.lm_head"
            return LINEAR_SHAPES.get((k, n), f"linear[{k}x{n}]")
        return "linear"

    if any(m in name for m in QUANT_MARKERS):
        return "quant"

    if "norm" in name.lower():
        return "norm"
    if "rotary" in name or "apply_rotary" in name:
        return "rope"
    if name.startswith("aten::") and any(
        a in name for a in ("silu", "gelu", "sigmoid", "silu_and_mul", "swiglu")
    ):
        return "activation"
    if any(a in name for a in ("argmax", "softmax", "topk", "gumbel", "multinomial")):
        return "sampler"
    if name.startswith("aten::"):
        return "elementwise"
    return "python"


def refine_stage(stage: str, op_class: str) -> str:
    """Overrides where the op itself is more informative than the enclosing frame."""
    if op_class == "linear.lm_head" and stage in ("model.forward", "runner.execute_model", "other"):
        return "model.lm_head"
    if op_class == "sampler" and stage in ("frontend.other", "runner.glue", "other"):
        return "sample"
    return stage


def classify(
    name: str,
    *,
    file: str = "",
    func: str = "",
    event_args: dict | None = None,
    vocab_size: int | None = None,
) -> Classified:
    """Full classification: engine stage + op class."""
    stage = stage_for_frame(file, func) if file else "other"
    op_class = op_class_for(name, event_args, vocab_size=vocab_size)
    return Classified(refine_stage(stage, op_class), op_class)
