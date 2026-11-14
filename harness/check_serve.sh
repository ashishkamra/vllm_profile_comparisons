#!/usr/bin/env bash
# Quick harness check: boot serve.sh for one variant, hit it once, shut down.
# Own the server process group; never kill an unrelated listener or use pkill.
set -eu
cd "$(dirname "${BASH_SOURCE[0]}")/.." || exit 1
source harness/env.sh

DEVICE=${DEVICE:?set DEVICE=cpu|gpu}
VARIANT=${VARIANT:?set VARIANT=ref|int8|fp8}
LOG=${SERVE_CHECK_LOG:-${TMPDIR:-/tmp}/serve-check.log}
TIMEOUT=${SERVE_CHECK_TIMEOUT:-600}
if ! [[ "$TIMEOUT" =~ ^[1-9][0-9]*$ ]]; then
  echo "SERVE_CHECK_TIMEOUT must be a positive integer (seconds)" >&2
  exit 2
fi
if ss -ltnH "sport = :$PORT" | grep -q .; then
  echo "port $PORT is already in use; stop the stale server before checking" >&2
  exit 1
fi

DEVICE="$DEVICE" VARIANT="$VARIANT" \
  setsid harness/serve.sh > "$LOG" 2>&1 < /dev/null &
SERVER_PID=$!
cleanup() {
  # setsid gives this launch its own group, including engine/compiler children.
  kill -TERM -- "-$SERVER_PID" 2>/dev/null || true
  for _ in 1 2 3 4 5; do
    kill -0 -- "-$SERVER_PID" 2>/dev/null || break
    sleep 1
  done
  kill -KILL -- "-$SERVER_PID" 2>/dev/null || true
  wait "$SERVER_PID" 2>/dev/null || true
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
fail() {
  echo "$*; server log: $LOG" >&2
  tail -80 "$LOG" >&2
  exit 1
}

echo "Starting $DEVICE/$VARIANT (pid $SERVER_PID); server log: $LOG"
START=$SECONDS
while true; do
  if ! kill -0 "$SERVER_PID" 2>/dev/null; then
    STATUS=0
    wait "$SERVER_PID" || STATUS=$?
    fail "server exited before becoming healthy (status $STATUS)"
  fi
  if curl -sf --connect-timeout 1 --max-time 2 "$TARGET/health" >/dev/null 2>&1; then
    echo "HEALTH UP after ~$((SECONDS-START))s"
    break
  fi
  if [ "$((SECONDS-START))" -ge "$TIMEOUT" ]; then
    fail "TIMEOUT after ${TIMEOUT}s waiting for $TARGET/health"
  fi
  sleep 2
done

echo "-- /v1/models"
curl -sf --max-time 60 "$TARGET/v1/models" || fail "model listing failed"
echo
echo "-- completion"
curl -sf --max-time 60 "$TARGET/v1/completions" -H 'Content-Type: application/json' \
  -d "{\"model\":\"$(model_for "$DEVICE" "$VARIANT")\",\"prompt\":\"The capital of France is\",\"max_tokens\":8,\"temperature\":0}" \
  || fail "completion failed"
echo

echo "-- metrics: preemptions"
METRICS=$(curl -sf --max-time 60 "$TARGET/metrics") || fail "metrics request failed"
awk '/^vllm:num_preemptions_total/ {s+=$2} END {print s+0}' <<< "$METRICS"

echo "check passed; shutting down server process group $SERVER_PID"
