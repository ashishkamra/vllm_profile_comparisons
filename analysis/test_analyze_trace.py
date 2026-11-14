# SPDX-License-Identifier: Apache-2.0
"""Tests for GPU kernel attribution. Run: pytest analysis/test_analyze_trace.py

Kernels run on a CUDA stream thread that holds no Python frames, so their stage
is taken from the host event that launched them, matched by `correlation`. Get
that wrong and the largest kernels in the trace silently become "other".
"""

import analyze_trace as A

MODEL_FILE = "vllm/model_executor/models/qwen3.py"


def frame(ts, dur, tid, func, file=MODEL_FILE):
    return {
        "ph": "X", "cat": "python_function", "tid": tid, "ts": ts, "dur": dur,
        "name": f"{file}:{func}",
    }


def kernel(ts, dur, corr, tid=7):
    return {
        "ph": "X", "cat": "kernel", "tid": tid, "ts": ts, "dur": dur,
        "name": GEMM_NAME, "args": {"correlation": corr, "stream": 7},
    }


def launch(cat, name, ts, corr, tid=1, dur=1.0):
    return {
        "ph": "X", "cat": cat, "tid": tid, "ts": ts, "dur": dur,
        "name": name, "args": {"correlation": corr},
    }


GEMM_NAME = "void cutlass::Kernel2<cutlass_80_wmma_tensorop_bf16_s161616gemm_bf16_32x32_128x2_tn_align8>(...)"


def stage_of(events, kern):
    index = A.StageIndex(events)
    launcher = A.kernel_to_launcher(events)
    host = launcher[id(kern)]
    return index.stage_at(host["tid"], host["ts"])


def test_driver_api_launches_are_followed():
    """cuBLAS launches GEMMs with cuLaunchKernel, not cudaLaunchKernel."""
    events = [
        frame(0.0, 100.0, 1, "forward"),
        launch("cuda_driver", "cuLaunchKernel", 10.0, 42),
        kernel(900.0, 50.0, 42),
    ]
    assert A.kernel_to_launcher(events)[id(events[2])]["cat"] == "cuda_driver"
    assert stage_of(events, events[2]) == "model.forward"


def test_runtime_api_launches_still_work():
    events = [
        frame(0.0, 100.0, 1, "forward"),
        launch("cuda_runtime", "cudaLaunchKernel", 10.0, 7),
        kernel(900.0, 50.0, 7),
    ]
    assert stage_of(events, events[2]) == "model.forward"


def test_unmatched_kernel_is_not_guessed():
    """No launcher means no frame: 'other', never a plausible-looking stage."""
    events = [frame(0.0, 100.0, 1, "forward"), kernel(900.0, 50.0, 99)]
    assert id(events[1]) not in A.kernel_to_launcher(events)
    assert A.StageIndex(events).stage_at(7, 900.0) == "other"


def test_correlation_is_matched_by_value_not_position():
    """Two launches, kernels out of launch order: each kernel keeps its own."""
    events = [
        frame(0.0, 100.0, 1, "forward"),
        launch("cuda_driver", "cuLaunchKernel", 10.0, 1),
        launch("cuda_driver", "cuLaunchKernelEx", 20.0, 2),
        kernel(950.0, 50.0, 2),
        kernel(900.0, 50.0, 1),
    ]
    m = A.kernel_to_launcher(events)
    assert m[id(events[3])]["ts"] == 20.0
    assert m[id(events[4])]["ts"] == 10.0


# ---------------------------------------------------------------------------
# Op-class refinement: a cutlass GEMM says "gemm", the enclosing aten::linear
# says which projection it is. Shapes come from the host op, not the kernel.
# ---------------------------------------------------------------------------

def cpu_op(ts, dur, tid, name, dims):
    return {
        "ph": "X", "cat": "cpu_op", "tid": tid, "ts": ts, "dur": dur,
        "name": name, "args": {"Input Dims": dims, "Input type": ["c10::BFloat16"]},
    }


def test_gpu_gemm_refined_by_enclosing_aten_linear():
    """(1024, 4096) is Qwen3's fused QKV; the kernel alone cannot know that."""
    host = cpu_op(0.0, 100.0, 1, "aten::linear", [[8, 1, 1024], [4096, 1024]])
    events = [host, launch("cuda_driver", "cuLaunchKernel", 10.0, 5),
              kernel(900.0, 50.0, 5)]
    enclosing = A.EnclosingOps(events)
    assert A.gpu_op_class(events[2], enclosing.op_at(1, 10.0), vocab_size=151936) == "linear.qkv"


def test_gpu_refinement_uses_the_aten_dims_convention():
    """Borrowing the custom-kernel layout would read (1024, 1024), not qkv.

    aten::linear records [input (..., K), weight (N, K)]; vLLM's custom kernels
    record [out (..., N), input (..., K), ...]. Refining through the host op's
    own name is what selects the right one -- silently reusing the kernel's
    convention would mislabel every projection instead of just coarsening it.
    """
    host = cpu_op(0.0, 100.0, 1, "aten::linear", [[8, 1, 1024], [4096, 1024]])
    events = [host, launch("cuda_driver", "cuLaunchKernel", 10.0, 5),
              kernel(900.0, 50.0, 5)]
    enclosing = A.EnclosingOps(events)
    assert A.gpu_op_class(events[2], enclosing.op_at(1, 10.0), vocab_size=151936) != "linear[1024x1024]"


def test_gpu_gemm_without_enclosing_op_stays_coarse():
    """No host op: stay at 'linear'. A guess would be worse than a coarse fact."""
    events = [launch("cuda_driver", "cuLaunchKernel", 10.0, 5),
              kernel(900.0, 50.0, 5)]
    assert A.gpu_op_class(events[1], None, vocab_size=151936) == "linear"


def test_gpu_gemm_with_non_linear_enclosing_op_stays_coarse():
    host = cpu_op(0.0, 100.0, 1, "aten::add", [[8, 1024], [8, 1024]])
    assert A.gpu_op_class(kernel(900.0, 50.0, 5), host, vocab_size=151936) == "linear"


def test_enclosing_op_returns_the_innermost_op():
    outer = cpu_op(0.0, 100.0, 1, "aten::linear", [[8, 1024], [4096, 1024]])
    inner = cpu_op(5.0, 20.0, 1, "aten::addmm", [[8, 1, 1024], [4096, 1024]])
    enclosing = A.EnclosingOps([outer, inner])
    assert enclosing.op_at(1, 6.0)["name"] == "aten::addmm"
    assert enclosing.op_at(1, 50.0)["name"] == "aten::linear"
    assert enclosing.op_at(1, 500.0) is None

