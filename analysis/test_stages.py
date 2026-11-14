# SPDX-License-Identifier: Apache-2.0
"""Tests for the stage taxonomy. Run: pytest analysis/test_stages.py"""

import stages


def test_engine_stages_from_frames():
    assert stages.stage_for_frame("vllm/v1/core/sched/scheduler.py", "schedule") == "engine.schedule"
    assert (
        stages.stage_for_frame("vllm/v1/core/sched/scheduler.py", "update_from_output")
        == "engine.postprocess"
    )
    assert (
        stages.stage_for_frame("vllm/v1/core/kv_cache_manager.py", "allocate_slots")
        == "engine.kv_cache"
    )


def test_worker_stages():
    assert (
        stages.stage_for_frame("vllm/v1/worker/gpu/model_runner.py", "prepare_inputs")
        == "runner.prepare_inputs"
    )
    assert (
        stages.stage_for_frame("vllm/v1/worker/cpu/sampler.py", "forward") == "sample"
    )
    assert (
        stages.stage_for_frame("vllm/model_executor/layers/linear.py", "forward")
        == "model.forward"
    )


def test_unknown_frame_is_other_never_guessed():
    assert stages.stage_for_frame("random/module.py", "fn") == "other"


def test_linear_shape_parsing():
    args = {"Input Dims": [[5, 1024], [6144, 1024]]}
    assert stages.linear_shape(args) == (1024, 6144)
    assert stages.op_class_for("aten::linear", args) == "linear.gate_up"
    assert stages.linear_shape({"Input Dims": []}) is None


def test_lm_head_by_vocab_size_and_stage_promotion():
    args = {"Input Dims": [[1, 1024], [151936, 1024]]}
    c = stages.classify(
        "aten::linear",
        file="vllm/model_executor/layers/linear.py",
        func="forward",
        event_args=args,
        vocab_size=151936,
    )
    assert c.op_class == "linear.lm_head"
    assert c.stage == "model.lm_head"


def test_attention_kernels_classified_on_both_devices():
    assert stages.op_class_for("cpu_attention_with_kv_cache") == "attention.paged"
    assert stages.op_class_for("flash_fwd_kernel") == "attention.paged"
    assert stages.op_class_for("cpu_attn_reshape_and_cache") == "attention.kv_write"


def test_elementwise_and_activation():
    assert stages.op_class_for("aten::silu") == "activation"
    assert stages.op_class_for("aten::mul") == "elementwise"
    assert stages.op_class_for("aten::rms_norm") == "norm"


def test_unknown_linear_shape_is_reported_not_merged():
    assert stages.op_class_for("aten::mm", {"Input Dims": [[4, 99], [77, 99]]}) == "linear[99x77]"
