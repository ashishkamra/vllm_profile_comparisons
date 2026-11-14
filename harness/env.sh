#!/usr/bin/env bash
# Pinned study configuration — the single source of truth for both legs.
# Every other script sources this file. Change values here, not there.

: "${R:=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
export R

# --- pins -------------------------------------------------------------------
export VLLM_COMMIT=7833dce84784045721e177716d5fa16079d76ff8
export VLLM_TAG=v0.34.0rc0
export GUIDELLM_PIN=guidellm==0.8.1

# --- server knobs (identical on both devices, see docs/02-methodology.md R5) -
export MAX_MODEL_LEN=${MAX_MODEL_LEN:-2048}
export MAX_NUM_SEQS=${MAX_NUM_SEQS:-16}
export GPU_MEM_UTIL=${GPU_MEM_UTIL:-0.85}
export PORT=${PORT:-8000}
export TARGET=${TARGET:-http://127.0.0.1:${PORT}}
export OMP_NUM_THREADS=${OMP_NUM_THREADS:-14}

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

SCENARIOS=${SCENARIOS:-"sync conc8 sweep"}
DATA_ARGS=${DATA_ARGS:-"kind=synthetic_text,prompt_tokens=512,output_tokens=128"}
