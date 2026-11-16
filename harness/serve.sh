#!/usr/bin/env bash
# Launch `vllm serve` for one (device, variant) with the study's pinned knobs.
#
#   DEVICE=cpu VARIANT=int8 harness/serve.sh
#   DEVICE=gpu VARIANT=fp8  ENFORCE_EAGER=1 harness/serve.sh      # for profiles
#   DEVICE=gpu VARIANT=fp8  PROFILE_DIR=$R/traces/gpu-fp8 harness/serve.sh
#
# PROFILE_DIR enables the /start_profile and /stop_profile endpoints.
set -eu
source "$(dirname "${BASH_SOURCE[0]}")/env.sh"

DEVICE=${DEVICE:?set DEVICE=cpu|gpu}
VARIANT=${VARIANT:?set VARIANT=ref|int8|fp8}

MODEL=$(model_for "$DEVICE" "$VARIANT") || { echo "unknown variant $DEVICE/$VARIANT" >&2; exit 2; }
DTYPE=$(dtype_for "$DEVICE")

# R3: cache precision is held constant across the study
ARGS=(
  serve "$MODEL"
  --dtype "$DTYPE"
  --max-model-len "$MAX_MODEL_LEN"
  --max-num-seqs "$MAX_NUM_SEQS"
  --port "$PORT"
  --host 127.0.0.1
  --kv-cache-dtype auto
)

if [ "$DEVICE" = "gpu" ]; then
  ARGS+=(--gpu-memory-utilization "$GPU_MEM_UTIL")
else
  export OMP_NUM_THREADS
fi

if [ "${ENFORCE_EAGER:-0}" = "1" ]; then
  ARGS+=(--enforce-eager)     # R6: op-level attribution, no CUDA graph replay
fi

if [ -n "${PROFILE_DIR:-}" ]; then
  mkdir -p "$PROFILE_DIR"
  # Where the model runs. `vllm serve` always keeps the API server and the engine
  # core in separate processes (AsyncLLM has no in-process mode). On the GPU the
  # model worker then runs inside the engine-core process (`uni` executor). On
  # the CPU, vllm/platforms/cpu.py moves it into a third, worker process (`mp`
  # executor) unless VLLM_ENABLE_V1_MULTIPROCESSING=0 -- and only the API server
  # and the worker carry a torch profiler, so the scheduler is not traced.
  # WORKER_IN_ENGINE=1 keeps the CPU worker inside the engine core, so one trace
  # holds the scheduler and the model; it is a profiling convenience that changes
  # the CPU process layout. WORKER_IN_ENGINE=0 profiles the layout vLLM really
  # uses. The variable has no effect on the GPU leg.
  if [ "${WORKER_IN_ENGINE:-1}" = "1" ]; then
    export VLLM_ENABLE_V1_MULTIPROCESSING=0
  fi
  if [ "$DEVICE" = "gpu" ]; then ACT='["CPU","CUDA"]'; else ACT='["CPU"]'; fi
  # record_shapes is OFF by default at 0.33. Without it the analyzer cannot tell
  # qkv from o_proj from lm_head (docs/03-stage-taxonomy.md axis B), so the whole
  # per-layer breakdown would collapse into "linear". It costs some overhead; it
  # is enabled identically on both legs.
  # PROFILE_SHAPES=0 drops shapes where they are not needed: exporting a long
  # CPU worker trace with shapes can take more than 16 GB.
  if [ "${PROFILE_SHAPES:-1}" = "1" ]; then SHAPES=true; else SHAPES=false; fi
  ARGS+=(--profiler-config "{\"profiler\":\"torch\",\"torch_profiler_dir\":\"$PROFILE_DIR\",\"torch_profiler_activities\":$ACT,\"torch_profiler_with_stack\":true,\"torch_profiler_record_shapes\":$SHAPES,\"torch_profiler_use_gzip\":true}")
fi

# Scenario-specific scheduler knobs (e.g. a small --max-num-batched-tokens so a
# chunked prefill is visible in a short trace). Word-split on purpose.
# shellcheck disable=SC2206
[ -n "${EXTRA_SERVE_ARGS:-}" ] && ARGS+=($EXTRA_SERVE_ARGS)

echo "+ vllm ${ARGS[*]}" >&2
exec "$PY" -m vllm.entrypoints.cli.main "${ARGS[@]}"
