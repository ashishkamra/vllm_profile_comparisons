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


def test_both_model_runners_are_recognised():
    # v0.33 ships V1 (flat file) and V2 (package); a run must be analysable either way
    v1 = "vllm/v1/worker/gpu_model_runner.py"
    v2 = "vllm/v1/worker/gpu/model_runner.py"
    for path in (v1, v2):
        assert stages.stage_for_frame(path, "_prepare_inputs") == "runner.prepare_inputs"
        assert stages.stage_for_frame(path, "execute_model") == "runner.execute_model"
        assert stages.stage_for_frame(path, "_dummy_run") == "runner.glue"


def test_cpu_runner_subclasses():
    assert (
        stages.stage_for_frame("vllm/v1/worker/cpu_model_runner.py", "execute_model")
        == "runner.execute_model"
    )
    assert (
        stages.stage_for_frame("vllm/v1/worker/cpu/model_runner.py", "prepare_inputs")
        == "runner.prepare_inputs"
    )


def test_unknown_frame_is_other_never_guessed():
    assert stages.stage_for_frame("random/module.py", "fn") == "other"


def test_linear_shape_parsing():
    args = {"Input Dims": [[5, 1024], [6144, 1024]]}
    assert stages.linear_shape("aten::linear", args) == (1024, 6144)
    assert stages.op_class_for("aten::linear", args) == "linear.gate_up"
    assert stages.linear_shape("aten::linear", {"Input Dims": []}) is None


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
    assert stages.op_class_for("aten::mm", {"Input Dims": [[4, 99], [99, 77]]}) == "linear[99x77]"


def test_aten_mm_uses_the_mat2_layout():
    # aten::mm is [mat1 (M, K), mat2 (K, N)], so N is the *last* dim. Parsing it
    # like aten::linear's weight (N, K) yields (K, K) -- square, plausible
    # looking, and wrong for every projection (seen on the GPU leg).
    assert stages.linear_shape("aten::mm", {"Input Dims": [[1, 1024], [1024, 6144]]}) == (1024, 6144)
    assert stages.op_class_for("aten::mm", {"Input Dims": [[1, 1024], [1024, 6144]]}) == "linear.gate_up"
    assert stages.op_class_for("aten::mm", {"Input Dims": [[1, 2048], [2048, 1024]]}) == "linear.o_proj"
    assert stages.op_class_for("aten::mm", {"Input Dims": [[1, 3072], [3072, 1024]]}) == "linear.down"


def test_aten_addmm_skips_the_bias():
    # addmm(bias, mat1, mat2): the bias is 1-D and first; the matrices are last.
    assert stages.op_class_for(
        "aten::addmm", {"Input Dims": [[4096], [8, 1024], [1024, 4096]]}
    ) == "linear.qkv"


def test_cpu_onednn_gemms_are_classified_by_shape():
    # The CPU leg emits no aten::linear. These dims are copied from a real
    # v0.33 CPU int8 trace (rank0): [out, input, scale, ..., weight_scale, ...].
    qkv = {"Input Dims": [[64, 4096], [64, 1024], [64, 1], [], [1, 4096], [], []]}
    assert stages.op_class_for("_C::onednn_scaled_mm", qkv) == "linear.qkv"

    gate_up = {"Input Dims": [[64, 6144], [64, 1024], [64, 1], [], [1, 6144], [], []]}
    assert stages.op_class_for("_C::onednn_scaled_mm", gate_up) == "linear.gate_up"

    # lm_head arrives as an fp32 mm with the vocabulary as the output width
    head = {"Input Dims": [[1, 151936], [1, 1024], [], []]}
    assert (
        stages.op_class_for("_C::onednn_mm", head, vocab_size=151936)
        == "linear.lm_head"
    )


def test_aten_linear_uses_the_other_dims_layout():
    # aten::linear is [input (..., K), weight (N, K)]
    ev = {"Input Dims": [[8, 1024], [4096, 1024]]}
    assert stages.op_class_for("aten::linear", ev) == "linear.qkv"


def test_int8_quantization_is_its_own_class():
    ev = {"Input Dims": [[64, 1024], [64, 1024], [64, 1], []]}
    assert stages.op_class_for("_C::dynamic_scaled_int8_quant", ev) == "quant"


def test_cpu_custom_ops_map_to_their_stage_ops():
    assert stages.op_class_for("_C::cpu_attention_with_kv_cache", {}) == "attention.paged"
    assert stages.op_class_for("_C::cpu_attn_reshape_and_cache", {}) == "attention.kv_write"
    assert stages.op_class_for("_C::rotary_embedding", {}) == "rope"
    assert stages.op_class_for("aten::silu", {}) == "activation"
