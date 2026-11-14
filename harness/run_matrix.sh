#!/usr/bin/env bash
# Full leg: every scenario for every variant on one device, then profile, analyse, plot.
#
#   DEVICE=cpu harness/run_matrix.sh
#   DEVICE=gpu harness/run_matrix.sh
#
# Run this on the machine that owns the accelerator. It is deliberately serial:
# never measure two variants at once on one host.
set -eu
source "$(dirname "${BASH_SOURCE[0]}")/env.sh"

DEVICE=${DEVICE:?set DEVICE=cpu|gpu}
# VARIANTS lets one variant be re-run without redoing the other (e.g. after a
# partial failure). Default is every variant the device defines.
VARIANTS=${VARIANTS:-$(variants_for "$DEVICE")}

"$R/harness/collect_env.sh" > "$R/results/env-$DEVICE-$(hostname).txt"
if [ "$DEVICE" = "cpu" ]; then
  # Record which instruction sets the leg actually executes (docs/02-methodology.md).
  "$R/harness/probe_isa.sh" > "$R/results/isa-$DEVICE-$(hostname).txt"
fi

# A leftover server would silently answer for the previous variant, so refuse to
# start a leg while the port is taken.
if ss -lptnH "sport = :$PORT" 2>/dev/null | grep -q .; then
  echo "port $PORT is already in use; stop the stale server before running a leg" >&2
  exit 1
fi

for VARIANT in $VARIANTS; do
  TAG="$DEVICE-$VARIANT"

  echo "=================== $TAG: throughput scenarios ==================="
  DEVICE="$DEVICE" VARIANT="$VARIANT" \
    "$R/harness/serve.sh" > "$R/results/$TAG.serve.log" 2>&1 &
  SERVER_PID=$!
  trap 'kill -TERM $SERVER_PID 2>/dev/null || true' EXIT

  for _ in $(seq 1 300); do
    curl -sf "$TARGET/health" >/dev/null 2>&1 && break
    kill -0 $SERVER_PID 2>/dev/null || { echo "server died:" >&2; tail -30 "$R/results/$TAG.serve.log" >&2; exit 1; }
    sleep 2
  done

  for SCENARIO in $SCENARIOS; do
    "$R/harness/run_scenario.sh" "$SCENARIO" "$TAG"
  done

  kill -TERM $SERVER_PID 2>/dev/null || true
  wait $SERVER_PID 2>/dev/null || true
  trap - EXIT

  echo "=================== $TAG: profiling conc8 ========================"
  DEVICE="$DEVICE" VARIANT="$VARIANT" "$R/harness/profile_scenario.sh" conc8
done

echo "=================== analysis ==================="
for VARIANT in $VARIANTS; do
  TAG="$DEVICE-$VARIANT"
  for SCENARIO in $SCENARIOS; do
    shopt -s nullglob
    TRACES=("$R/traces/$TAG-$SCENARIO"/*.json.gz)
    shopt -u nullglob
    [ ${#TRACES[@]} -eq 0 ] && continue
    "$ANALYSIS_PY" "$R/analysis/analyze_trace.py" --trace "${TRACES[@]}" \
      --out "$R/results/$TAG-$SCENARIO-stages.csv"
  done
done

"$ANALYSIS_PY" "$R/analysis/plot_compare.py" "$R"/results/*-stages.csv --out "$R/figures" || true
"$ANALYSIS_PY" "$R/harness/summarize.py" --results "$R/results" --out "$R/results/summary.csv" || true
echo "done. figures in $R/figures, metrics in $R/results"
