#!/usr/bin/env bash
# Optional CPU-leg cross-check: sample the serving process with py-spy while the
# same GuideLLM scenario runs, so the Python breakdown can be compared against
# the instrumented torch profile (which perturbs what it measures).
#
#   DEVICE=cpu VARIANT=int8 harness/pyspy_cpu.sh conc8
#
# py-spy is started as the *parent* of `vllm serve`, which avoids needing
# CAP_SYS_PTRACE/yama relaxations. The output is a speedscope file, one per run.
set -eu
source "$(dirname "${BASH_SOURCE[0]}")/env.sh"

SCENARIO=${1:?usage: pyspy_cpu.sh <sync|conc8|sweep>}
DEVICE=${DEVICE:?set DEVICE=cpu}
VARIANT=${VARIANT:?set VARIANT=ref|int8}
TAG="$DEVICE-$VARIANT"
OUT="$R/results/$TAG-$SCENARIO.speedscope.json"
LOG="$R/results/$TAG-$SCENARIO.pyspy.log"

PYSPY=$(command -v py-spy || echo "$HOME/.local/share/bin/py-spy")
[ -x "$PYSPY" ] || { echo "py-spy not found; skipping" >&2; exit 0; }

mkdir -p "$R/results"

MODEL=$(model_for "$DEVICE" "$VARIANT")
# No profiler: this run exists to see the server as it normally behaves.
# --subprocesses matters: the engine core runs in a separate mp process, which
# is where the scheduler and the model forward actually execute.
"$PYSPY" record --format speedscope --rate 100 --subprocesses --output "$OUT" -- \
  "$PY" -m vllm.entrypoints.cli.main serve "$MODEL" \
  --dtype "$(dtype_for "$DEVICE")" \
  --max-model-len "$MAX_MODEL_LEN" --max-num-seqs "$MAX_NUM_SEQS" \
  --port "$PORT" --host 127.0.0.1 --kv-cache-dtype auto \
  > "$LOG" 2>&1 &
SPID=$!
trap 'kill -TERM $SPID 2>/dev/null || true' EXIT

for _ in $(seq 1 300); do
  curl -sf "$TARGET/health" >/dev/null 2>&1 && break
  kill -0 $SPID 2>/dev/null || { echo "server died, see $LOG" >&2; exit 1; }
  sleep 2
done

"$R/harness/run_scenario.sh" "$SCENARIO" "$TAG"

# SIGTERM lets py-spy flush the speedscope file before the server goes away.
kill -TERM $SPID 2>/dev/null || true
wait $SPID 2>/dev/null || true
trap - EXIT
echo "+ wrote $OUT" >&2
