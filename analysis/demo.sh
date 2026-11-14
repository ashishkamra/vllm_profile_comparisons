#!/usr/bin/env bash
# Pipeline smoke test: no GPU, no vLLM, no model. Proves the analyzer and the
# plotting work on this machine before you spend 40 minutes building anything.
set -eu
cd "$(dirname "${BASH_SOURCE[0]}")"
OUT=${1:-/tmp/vllm-profile-demo}
mkdir -p "$OUT"
python3 analyze_trace.py --trace fixtures/synthetic-trace.json --out "$OUT/demo-stages.csv"
python3 plot_compare.py "$OUT/demo-stages.csv" --out "$OUT/figures"
ls -la "$OUT/figures"
