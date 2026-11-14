#!/usr/bin/env bash
# Quick harness check: boot serve.sh for one variant, hit it once, shut down.
# Deliberately avoids pkill patterns that would match the caller's own command line.
set -eu
cd "$(dirname "${BASH_SOURCE[0]}")/.." || exit 1
source harness/env.sh

DEVICE=${DEVICE:?set DEVICE=cpu} VARIANT=${VARIANT:?set VARIANT=ref} \
  setsid nohup harness/serve.sh > /tmp/opencode/serve-check.log 2>&1 < /dev/null &

for i in $(seq 1 300); do
  if curl -sf "$TARGET/health" >/dev/null 2>&1; then echo "HEALTH UP after ~$((i*2))s"; break; fi
  if ! ss -lptn "sport = :$PORT" 2>/dev/null | grep -q LISTEN; then
    if [ "$i" -gt 30 ]; then echo "no listener at ~$((i*2))s"; fi
  fi
  sleep 2
  if [ "$i" -eq 300 ]; then echo "TIMEOUT"; exit 1; fi
done

echo "-- /v1/models"
curl -sf "$TARGET/v1/models" | head -c 300; echo
echo "-- completion"
curl -sf "$TARGET/v1/completions" -H 'Content-Type: application/json' \
  -d "{\"model\":\"$(model_for "$DEVICE" "$VARIANT")\",\"prompt\":\"The capital of France is\",\"max_tokens\":8,\"temperature\":0}" \
  | head -c 400; echo

echo "-- metrics: preemptions"
curl -sf "$TARGET/metrics" | awk '/^vllm:num_preemptions_total/ {s+=$2} END {print s+0}'

# Shut down by port owner, not by pattern.
PIDS=$(ss -lptnH "sport = :$PORT" | grep -oP 'pid=\K[0-9]+' | sort -u)
for p in $PIDS; do kill -TERM "$p" 2>/dev/null || true; done
sleep 3
echo "shutdown sent to: ${PIDS:-none}"
