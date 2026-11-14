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
        "name": "void cutlass::Kernel2<...>", "args": {"correlation": corr, "stream": 7},
    }


def launch(cat, name, ts, corr, tid=1, dur=1.0):
    return {
        "ph": "X", "cat": cat, "tid": tid, "ts": ts, "dur": dur,
        "name": name, "args": {"correlation": corr},
    }


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
