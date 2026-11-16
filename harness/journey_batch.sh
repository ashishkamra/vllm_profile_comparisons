#!/usr/bin/env bash
# Run several journey captures back to back, with a cool-down between them.
#
#   DEVICE=gpu VARIANT=fp8 harness/journey_batch.sh "default single" "eager single"
#
# With no arguments: all four mode x scenario combinations. Each spec is
# "<MODE> <SCENARIO>"; a failed capture is reported and the batch moves on.
set -u
cd "$(dirname "${BASH_SOURCE[0]}")/.." || exit 1
: "${DEVICE:?set DEVICE=cpu|gpu}"
VARIANT=${VARIANT:-ref}
COOL=${COOL:-30}
[ $# -eq 0 ] && set -- "default single" "eager single" "eager staggered" "default staggered"
for spec in "$@"; do
  read -r mode scen <<<"$spec"
  echo "=== $DEVICE $VARIANT $mode $scen $(date +%T)"
  DEVICE="$DEVICE" VARIANT="$VARIANT" MODE="$mode" SCENARIO="$scen" harness/journey_run.sh \
    || echo "FAILED $mode $scen"
  sleep "$COOL"
done
echo ALL_DONE
