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
  if [ "$DEVICE" = "gpu" ]; then ACT='["CPU","CUDA"]'; else ACT='["CPU"]'; fi
  ARGS+=(--profiler-config "{\"profiler\":\"torch\",\"torch_profiler_dir\":\"$PROFILE_DIR\",\"torch_profiler_activities\":$ACT,\"torch_profiler_with_stack\":true,\"torch_profiler_use_gzip\":true}")
fi

echo "+ vllm ${ARGS[*]}" >&2
exec vllm "${ARGS[@]}"
