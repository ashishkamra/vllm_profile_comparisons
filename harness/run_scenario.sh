#!/usr/bin/env bash
# Run one GuideLLM scenario against an already-running server.
#   harness/run_scenario.sh sync cpu-int8
# Results land in results/<tag>-<scenario>/benchmarks.{json,csv}
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/env.sh"

SCENARIO=${1:?usage: run_scenario.sh <sync|conc8|sweep> <tag>}
TAG=${2:?usage: run_scenario.sh <sync|conc8|sweep> <tag>}
# TAG is "<device>-<variant>"; the backend spec needs both to name the model.
DEVICE=${DEVICE:-${TAG%%-*}}
VARIANT=${VARIANT:-${TAG#*-}}
EXTRA=$(scenario_args "$SCENARIO") || { echo "unknown scenario $SCENARIO" >&2; exit 2; }
OUT="$R/results/$TAG-$SCENARIO"

mkdir -p "$OUT"
curl -sf -X POST "$TARGET/reset_prefix_cache" >/dev/null || true

# preemption count before/after: a non-zero delta invalidates cross-device arithmetic
PREEMPT_BEFORE=$(curl -sf "$TARGET/metrics" 2>/dev/null \
  | awk '/^vllm:num_preemptions_total/ {s+=$2} END {print s+0}' || true)

echo "+ guidellm run $SCENARIO ($TAG) -> $OUT" >&2
GUIDELLM__DEFAULT_RESULTS_DIR="$OUT" "$(guidellm_bin)" run \
  --backend "$(backend_args "$DEVICE" "$VARIANT")" \
  $EXTRA \
  --data "$DATA_ARGS" \
  | tee "$OUT/console.txt"

PREEMPT_AFTER=$(curl -sf "$TARGET/metrics" 2>/dev/null \
  | awk '/^vllm:num_preemptions_total/ {s+=$2} END {print s+0}' || true)
PREEMPT_AFTER=${PREEMPT_AFTER:-0}
PREEMPT_BEFORE=${PREEMPT_BEFORE:-0}

PREEMPT_DELTA=$(( ${PREEMPT_AFTER%%.*} - ${PREEMPT_BEFORE%%.*} ))
echo "preemptions_delta=$PREEMPT_DELTA" | tee -a "$OUT/console.txt"
if [ "${PREEMPT_DELTA#-}" != "0" ]; then
  echo "WARNING: $SCENARIO on $TAG preempted $PREEMPT_DELTA times — see docs/02-methodology.md" >&2
  echo "PREEMPT>0" > "$OUT/INVALID-preempted"
fi
