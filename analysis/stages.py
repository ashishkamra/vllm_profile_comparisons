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
        "v1/engine/inputs/",
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
    # worker
    ("runner.prepare_inputs", (
        "v1/worker/gpu/model_runner.py",
        "v1/worker/cpu/attn_utils.py",
        "v1/worker/cpu/input_batch.py",
    )),
    ("sample", (
        "v1/worker/gpu/sample/",
        "v1/worker/cpu/sampler.py",
        "v1/sample/ops/",
        "layers/sampler.py",
    )),
    # model
    ("model.forward", (
        "model_executor/models/",
        "model_executor/layers/",
        "model_executor/kernels/",
    )),
]

# Frames that live in a file that also hosts scheduler work (update_from_output etc.)
POSTPROCESS_MARKERS = ("update_from_output", "_free_request", "_update_request_with_output")

# ---------------------------------------------------------------------------
# Axis B: op class, from ATen op name and (optionally) tensor shapes.
# ---------------------------------------------------------------------------
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
    (1024, 2048): "linear.qkv",
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


def linear_shape(event_args: dict) -> tuple[int, int] | None:
    """(K, N) for an aten::linear / aten::mm / aten::addmm event, else None."""
    dims = _dims_of(event_args)
    if not dims or len(dims) < 2:
        return None
    try:
        in_dims, weight_dims = dims[0], dims[1]
        if not in_dims or not weight_dims:
            return None
        return int(in_dims[-1]), int(weight_dims[0])
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

    if name.startswith(("aten::linear", "aten::mm", "aten::addmm", "aten::matmul", "gemm")):
        shape = linear_shape(event_args)
        if shape is not None:
            k, n = shape
            if vocab_size and n >= int(0.9 * vocab_size):
                return "linear.lm_head"
            return LINEAR_SHAPES.get((k, n), f"linear[{k}x{n}]")
        return "linear"

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
    if op_class == "linear.lm_head" and stage in ("model.forward", "other"):
        return "model.lm_head"
    if op_class == "sampler" and stage in ("frontend.other", "other"):
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
