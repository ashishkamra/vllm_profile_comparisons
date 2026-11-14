#!/usr/bin/env bash
# Pinned study configuration — the single source of truth for both legs.
# Every other script sources this file. Change values here, not there.

: "${R:=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
export R

# --- interpreter and entrypoints --------------------------------------------
# Never rely on PATH: a stale vllm in user site-packages would silently win and
# the study would measure a different version than the pin says.
export VLLM_SRC=${VLLM_SRC:-$HOME/workspace/vllm}
export PY=${PY:-$VLLM_SRC/.venv/bin/python}
[ -x "$PY" ] || PY=$(command -v python3 || echo python)
export PY

vllm_cli() { "$PY" -m vllm.entrypoints.cli.main "$@"; }

guidellm_bin() {
  if [ -x "$(dirname "$PY")/guidellm" ]; then
    echo "$(dirname "$PY")/guidellm"
  else
    command -v guidellm
  fi
}

# --- pins -------------------------------------------------------------------
export VLLM_TAG=v0.33.0
export VLLM_COMMIT=4ba787ec9959e566d3b587a796dff07673324da5
export VLLM_PYPI=vllm==0.33.0          # what the GPU leg installs
export GUIDELLM_PIN=guidellm==0.8.1

# v0.33.0 ships two model runners and picks one implicitly:
#   V2 requires Triton -> default on the GPU, but Triton is disabled on a
#   CPU-only host, so the CPU would silently fall back to V1.
# Two different runners means two different Python frame structures, which would
# silently break the stage comparison. So pin the common denominator, V1.
#   0 = V1 (vllm/v1/worker/gpu_model_runner.py)   <- both legs
#   1 = V2 (vllm/v1/worker/gpu/model_runner.py, the 0.34 default)
export VLLM_USE_V2_MODEL_RUNNER=${VLLM_USE_V2_MODEL_RUNNER:-0}

# --- server knobs (identical on both devices, see docs/02-methodology.md R5) -
export MAX_MODEL_LEN=${MAX_MODEL_LEN:-2048}
export MAX_NUM_SEQS=${MAX_NUM_SEQS:-16}
export GPU_MEM_UTIL=${GPU_MEM_UTIL:-0.85}
export PORT=${PORT:-8000}
export TARGET=${TARGET:-http://127.0.0.1:${PORT}}
# CPU leg: pin the thread count and KV-cache budget explicitly. vLLM honours an
# externally-set OMP_NUM_THREADS, so this is what the CPU numbers are measured at.
export OMP_NUM_THREADS=${OMP_NUM_THREADS:-$(nproc)}
export VLLM_CPU_KVCACHE_SPACE=${VLLM_CPU_KVCACHE_SPACE:-4}   # GiB, vLLM's documented default
export VLLM_CPU_OMP_THREADS_BIND=${VLLM_CPU_OMP_THREADS_BIND:-auto}

# --- model matrix -----------------------------------------------------------
# variant names are the tags used in results/ and figures/ everywhere.
model_for() {  # model_for <device> <variant>
  case "$1:$2" in
    cpu:ref)  echo "Qwen/Qwen3-0.6B" ;;
    cpu:int8) echo "nytopop/Qwen3-0.6B.w8a8" ;;
    gpu:ref)  echo "Qwen/Qwen3-0.6B" ;;
    gpu:fp8)  echo "RedHatAI/Qwen3-0.6B-FP8-dynamic" ;;
    *) return 1 ;;
  esac
}

# Reference dtype is device-optimal, not identical: the CPU has no bf16 hardware.
dtype_for() {
  case "$1" in
    cpu) echo "float32" ;;
    gpu) echo "bfloat16" ;;
    *) return 1 ;;
  esac
}

variants_for() {
  case "$1" in
    cpu) echo "ref int8" ;;
    gpu) echo "ref fp8" ;;
    *) return 1 ;;
  esac
}

# --- scenarios (identical on both devices, see docs/02-methodology.md R4) ----
# prints: "<extra guidellm args>"
scenario_args() {
  case "$1" in
    sync)   echo "--profile kind=synchronous --constraint kind=max_requests,count=60" ;;
    conc8)  echo "--profile kind=concurrent,streams=8 --constraint kind=max_duration,seconds=60" ;;
    sweep)  echo "--profile kind=sweep --constraint kind=max_duration,seconds=30" ;;
    *) return 1 ;;
  esac
}

# GuideLLM backend spec. request_format is pinned to /v1/completions (not the
# chat default) so prompt_tokens=512 is exactly 512 tokens with no chat-template
# overhead, on both legs.
backend_args() {  # backend_args <device> <variant>
  echo "kind=openai_http,target=$TARGET,model=$(model_for "$1" "$2"),request_format=/v1/completions"
}

SCENARIOS=${SCENARIOS:-"sync conc8 sweep"}
DATA_ARGS=${DATA_ARGS:-"kind=synthetic_text,prompt_tokens=512,output_tokens=128"}
