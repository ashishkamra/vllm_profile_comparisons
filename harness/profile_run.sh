#!/usr/bin/env bash
# Profile one (device, variant) with a tiny serial request set instead of GuideLLM.
#
#   DEVICE=cpu VARIANT=ref harness/profile_run.sh
#   DEVICE=gpu VARIANT=ref harness/profile_run.sh
#
# Same trace conditions as profile_scenario.sh -- enforce-eager, CPU worker kept in the
# engine core (WORKER_IN_ENGINE, see serve.sh),
# max_iterations cap, record_shapes -- because docs/02-methodology.md R6/R9 apply
# to the trace, not to the load. The difference is only what drives the window:
# a handful of serial requests, sized so the profiler's iteration cap is reached
# in seconds rather than by running a laptop GPU at maximum for a minute.
#
# Knobs (defaults are the study shape: 512-token prompt, 128-token completion):
#   PROFILE_REQUESTS=4 PROFILE_PROMPT_TOKENS=512 PROFILE_MAX_TOKENS=128
#   PROFILE_ITERS=20        engine iterations the profiler records
#   GPU_TEMP_MAX=80         refuse to start above this; 0 disables the check
set -eu
cd "$(dirname "${BASH_SOURCE[0]}")/.." || exit 1
source harness/env.sh

DEVICE=${DEVICE:?set DEVICE=cpu|gpu}
VARIANT=${VARIANT:?set VARIANT=ref|int8|fp8}
TAG="$DEVICE-$VARIANT"
SCENARIO=${PROFILE_SCENARIO:-profile}
TRACE_DIR="$R/traces/$TAG-$SCENARIO"
LOG="$R/results/$TAG-$SCENARIO.serve.log"   # matches the results/*.serve.log ignore
MODEL=$(model_for "$DEVICE" "$VARIANT")

PROFILE_REQUESTS=${PROFILE_REQUESTS:-4}
PROFILE_PROMPT_TOKENS=${PROFILE_PROMPT_TOKENS:-512}
PROFILE_MAX_TOKENS=${PROFILE_MAX_TOKENS:-128}
PROFILE_ITERS=${PROFILE_ITERS:-20}
GPU_TEMP_MAX=${GPU_TEMP_MAX:-80}

gpu_temp() {
  nvidia-smi --query-gpu=temperature.gpu --format=csv,noheader,nounits 2>/dev/null \
    | head -1 | tr -d ' '
}

if [ "$DEVICE" = "gpu" ] && [ "$GPU_TEMP_MAX" != "0" ]; then
  T=$(gpu_temp)
  if [ -n "$T" ] && [ "$T" -ge "$GPU_TEMP_MAX" ]; then
    echo "GPU is at ${T}C (limit ${GPU_TEMP_MAX}C); let it cool before profiling." >&2
    echo "This host has locked up under sustained load before -- that is what this" >&2
    echo "check is for. Override with GPU_TEMP_MAX=0 only if you know it is idle." >&2
    exit 1
  fi
  echo "+ GPU at ${T:-unknown}C before start (limit ${GPU_TEMP_MAX}C)" >&2
fi

if ss -ltnH "sport = :$PORT" | grep -q .; then
  echo "port $PORT is already in use; stop the stale server first" >&2
  exit 1
fi

mkdir -p "$R/results" "$TRACE_DIR"
rm -f "$TRACE_DIR"/*.pt.trace.json* 2>/dev/null || true

echo "+ booting $TAG (enforce-eager, worker in engine core, profiler on); log: $LOG" >&2
DEVICE="$DEVICE" VARIANT="$VARIANT" ENFORCE_EAGER=1 PROFILE_DIR="$TRACE_DIR" \
  "$R/harness/serve.sh" > "$LOG" 2>&1 &
SERVER_PID=$!
cleanup() {
  kill -TERM "$SERVER_PID" 2>/dev/null || true
  wait "$SERVER_PID" 2>/dev/null || true
}
trap cleanup EXIT

echo -n "+ waiting for $TARGET/health " >&2
for _ in $(seq 1 300); do
  if curl -sf "$TARGET/health" >/dev/null 2>&1; then echo " up" >&2; break; fi
  if ! kill -0 "$SERVER_PID" 2>/dev/null; then
    echo; echo "server died; log $LOG:" >&2; tail -40 "$LOG" >&2; exit 1
  fi
  echo -n "."; sleep 2
done

# Warm up outside the traced window: allocator, prefix cache, lazily-imported code.
# The preamble makes this text different from the first token on, so even if the
# reset below does not take effect the timed prompt cannot hit its cache blocks.
"$PY" "$R/harness/drive_requests.py" --target "$TARGET" --model "$MODEL" \
  --requests 1 --max-tokens 8 --prompt-tokens 32 \
  --preamble "Warmup request; distinct prefix, not part of the measured prompt. " >/dev/null
curl -sf -X POST "$TARGET/reset_prefix_cache" >/dev/null || true

echo "+ start_profile (max_iterations=$PROFILE_ITERS)" >&2
curl -sf -X POST "$TARGET/start_profile" -H 'Content-Type: application/json' \
  -d "{\"max_iterations\": $PROFILE_ITERS}" >/dev/null

"$PY" "$R/harness/drive_requests.py" --target "$TARGET" --model "$MODEL" \
  --requests "$PROFILE_REQUESTS" --max-tokens "$PROFILE_MAX_TOKENS" \
  --prompt-tokens "$PROFILE_PROMPT_TOKENS" \
  --out "$R/results/$TAG-$SCENARIO.requests.json"

# The profiler stops itself at the iteration cap; this call only finalises.
echo "+ stop_profile" >&2
curl -sf -X POST "$TARGET/stop_profile" >/dev/null || echo "WARN: stop_profile returned non-2xx" >&2
sleep 5   # let the trace handler finish writing

cleanup
trap - EXIT

if [ "$DEVICE" = "gpu" ]; then
  echo "+ GPU at $(gpu_temp) C after run" >&2
fi

echo "+ trace in $TRACE_DIR:" >&2
ls -la "$TRACE_DIR" >&2
if ! ls "$TRACE_DIR"/rank0*.pt.trace.json* >/dev/null 2>&1; then
  echo "no engine-core trace (rank0*) was written -- check $LOG" >&2
  exit 1
fi

# What the scheduler actually scheduled, read from its own annotation. A prefix
# cache hit or a chunked prefill shows up here as a number below the prompt
# size; without this check it only surfaced later, as a puzzling 480 in the
# analysis of a 512-token request.
"$PY" - "$TRACE_DIR" "$PROFILE_PROMPT_TOKENS" <<'PY'
import glob, gzip, json, re, sys
trace_dir, want = sys.argv[1], int(sys.argv[2])
step = re.compile(r"execute_context_(\d+)\((\d+)\)_generation_(\d+)\((\d+)\)")
path = sorted(glob.glob(f"{trace_dir}/rank0*.json.gz"))[0]
with gzip.open(path, "rt") as f:
    raw = json.load(f)
events = raw["traceEvents"] if isinstance(raw, dict) else raw
ann = sorted(
    (e for e in events if e.get("cat") == "user_annotation" and step.match(e.get("name", ""))),
    key=lambda e: e["ts"],
)
if not ann:
    print("prefill step: no execute_context_* annotation found", file=sys.stderr)
    sys.exit(0)
ctx = int(step.match(ann[0]["name"]).group(2))
print(f"prefill step: {ctx} context tokens (prompt asked for {want})")
if ctx != want:
    print(f"WARN: {want - ctx} prompt tokens were not scheduled as context "
          "(prefix-cache hit or chunked prefill); the legs are not identical",
          file=sys.stderr)
PY
