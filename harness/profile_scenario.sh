#!/usr/bin/env bash
# Profile one scenario end to end: boot server -> warm -> /start_profile ->
# GuideLLM -> /stop_profile -> trace on disk.
#
#   DEVICE=cpu VARIANT=int8 harness/profile_scenario.sh conc8
#
# Profiles always run with --enforce-eager (docs/02-methodology.md R6).
set -eu
source "$(dirname "${BASH_SOURCE[0]}")/env.sh"

SCENARIO=${1:?usage: profile_scenario.sh <sync|conc8|sweep>}
DEVICE=${DEVICE:?set DEVICE=cpu|gpu}
VARIANT=${VARIANT:?set VARIANT=ref|int8|fp8}
TAG="$DEVICE-$VARIANT"
TRACE_DIR="$R/traces/$TAG-$SCENARIO"
PROFILE_ITERS=${PROFILE_ITERS:-20}
LOG="$R/results/$TAG-$SCENARIO.profile.log"

mkdir -p "$R/results" "$TRACE_DIR"

echo "+ booting server (enforce-eager, profiler on)" >&2
DEVICE="$DEVICE" VARIANT="$VARIANT" ENFORCE_EAGER=1 PROFILE_DIR="$TRACE_DIR" \
  "$R/harness/serve.sh" > "$LOG" 2>&1 &
SERVER_PID=$!
trap 'kill -TERM $SERVER_PID 2>/dev/null || true' EXIT

echo -n "+ waiting for $TARGET/health " >&2
for _ in $(seq 1 300); do
  if curl -sf "$TARGET/health" >/dev/null 2>&1; then echo " up" >&2; break; fi
  if ! kill -0 "$SERVER_PID" 2>/dev/null; then
    echo; echo "server died, see $LOG" >&2; tail -30 "$LOG" >&2; exit 1
  fi
  echo -n "." ; sleep 2
done

# warm up outside the traced window: allocator, prefix cache, lazily-imported paths
curl -sf -X POST "$TARGET/reset_prefix_cache" >/dev/null || true
for _ in 1 2 3; do
  curl -sf "$TARGET/v1/completions" -H 'Content-Type: application/json' \
    -d '{"model":"'"$(model_for "$DEVICE" "$VARIANT")"'","prompt":"warmup","max_tokens":8,"temperature":0}' \
    >/dev/null || true
done
curl -sf -X POST "$TARGET/reset_prefix_cache" >/dev/null || true

# Bound the trace by engine iterations, not wall clock. An unbounded concurrent
# profile produces a trace so large that /stop_profile times out on the RPC and
# the worker trace is never written.
echo "+ start_profile (max_iterations=$PROFILE_ITERS)" >&2
curl -sf -X POST "$TARGET/start_profile" -H 'Content-Type: application/json' \
  -d "{\"max_iterations\": $PROFILE_ITERS}" >/dev/null

EXTRA=$(scenario_args "$SCENARIO")
GUIDELLM__DEFAULT_RESULTS_DIR="$R/results/$TAG-$SCENARIO" "$(guidellm_bin)" run \
  --backend "$(backend_args "$DEVICE" "$VARIANT")" \
  $EXTRA \
  --data "$DATA_ARGS" \
  > "$R/results/$TAG-$SCENARIO.profile.console.txt" 2>&1

# With max_iterations the profiler stops itself and writes the trace; this call
# is then cheap and just finalises.
echo "+ stop_profile" >&2
curl -sf -X POST "$TARGET/stop_profile" >/dev/null || echo "WARN: stop_profile returned non-2xx" >&2
sleep 5   # let the trace handler finish writing to disk

kill -TERM $SERVER_PID 2>/dev/null || true
wait $SERVER_PID 2>/dev/null || true
trap - EXIT

echo "+ traces:" >&2
ls -la "$TRACE_DIR" >&2
