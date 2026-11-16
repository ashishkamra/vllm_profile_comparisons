#!/usr/bin/env bash
# Profile how vLLM moves requests through its processes, on one device.
#
#   DEVICE=cpu MODE=default SCENARIO=single    harness/journey_run.sh
#   DEVICE=gpu MODE=eager   SCENARIO=staggered harness/journey_run.sh
#
# MODE
#   default  vLLM's own configuration for the device: no --enforce-eager, and the
#            real process layout (on the CPU: API server + engine core + a separate
#            worker process). torch.compile is on for both devices, and CUDA graphs
#            and async scheduling on the GPU. This is how vLLM actually runs.
#   eager    --enforce-eager (no compile, no CUDA graphs), so every op and kernel
#            is visible and attributable. On the CPU the worker is kept inside the
#            engine-core process (WORKER_IN_ENGINE=1) so one trace shows the
#            scheduler and the model together.
# SCENARIO   single | staggered   (see harness/drive_journey.py)
#
# Outputs
#   traces/journey/<tag>/      torch traces (API server + engine core or worker),
#                              plus a py-spy chrometrace of the CPU engine core in
#                              default mode, where it has no torch profiler
#   results/journey/<tag>.client.json    per-token arrival times at the client
#   results/journey/<tag>.manifest.json  process roles/PIDs, effective config
#   results/journey/<tag>.serve.log      the server's own log (startup included)
#
# The load is seconds long on purpose: this host has overheated under sustained
# load, so the GPU leg refuses to start above GPU_TEMP_MAX.
set -eu
cd "$(dirname "${BASH_SOURCE[0]}")/.." || exit 1
source harness/env.sh

DEVICE=${DEVICE:?set DEVICE=cpu|gpu}
VARIANT=${VARIANT:-ref}
MODE=${MODE:?set MODE=default|eager}
SCENARIO=${SCENARIO:?set SCENARIO=single|staggered}
# Reference-weight runs keep the short tag; quantized runs carry the variant.
if [ "$VARIANT" = "ref" ]; then TAG="$DEVICE-$MODE-$SCENARIO"; else TAG="$DEVICE-$VARIANT-$MODE-$SCENARIO"; fi
TRACE_DIR="$R/traces/journey/$TAG"
RES_DIR="$R/results/journey"
LOG="$RES_DIR/$TAG.serve.log"
MODEL=$(model_for "$DEVICE" "$VARIANT")
GPU_TEMP_MAX=${GPU_TEMP_MAX:-80}

case "$SCENARIO" in
  single)    ITERS=${PROFILE_ITERS:-60};  SCEN_ARGS="" ;;
  # A small token budget so B's 1024-token prompt is visibly chunked across steps
  # that also carry other requests' decodes.
  staggered) ITERS=${PROFILE_ITERS:-120}; SCEN_ARGS="--max-num-batched-tokens 512" ;;
  # KV-cache scenarios: the block-level probe is on by default.
  prefix)    ITERS=${PROFILE_ITERS:-120}; SCEN_ARGS=""; KV_PROBE=${KV_PROBE:-1}
             export PROFILE_SHAPES=${PROFILE_SHAPES:-0} ;;
  # A pool of 2,176 tokens on both devices (17 usable blocks of 128 on the CPU,
  # 136 of 16 on the GPU, plus vLLM's reserved null block 0). ~150 steps: too
  # long for a torch trace on the CPU, so the profiler is off by default and the
  # probe tells the story.
  pressure)  ITERS=${PROFILE_ITERS:-200}; KV_PROBE=${KV_PROBE:-1}; PROFILE=${PROFILE:-0}
             if [ "$DEVICE" = "cpu" ]; then NBLK=18; else NBLK=137; fi
             SCEN_ARGS="--num-gpu-blocks-override $NBLK" ;;
  *) echo "unknown SCENARIO $SCENARIO" >&2; exit 2 ;;
esac
PROFILE=${PROFILE:-1}
KV_PROBE=${KV_PROBE:-0}
PROBE_OUT=""
if [ "$KV_PROBE" = "1" ]; then
  # harness/kvprobe/sitecustomize.py: one JSON line per engine step with every
  # running request's block table (see that file).
  PROBE_OUT="$RES_DIR/$TAG.kvprobe.jsonl"
  mkdir -p "$RES_DIR"; rm -f "$PROBE_OUT"
  export PYTHONPATH="$R/harness/kvprobe${PYTHONPATH:+:$PYTHONPATH}"
  export VLLM_KV_PROBE="$PROBE_OUT"
  # Named sub-ranges inside schedule() ("schedule: allocate_slots", ...).
  export VLLM_CUSTOM_SCOPES_FOR_PROFILING=1
fi
case "$MODE" in
  default) EAGER=0; WORKER_IN_ENGINE=0 ;;
  eager)   EAGER=1; WORKER_IN_ENGINE=1 ;;
  *) echo "unknown MODE $MODE" >&2; exit 2 ;;
esac

gpu_temp() {
  nvidia-smi --query-gpu=temperature.gpu --format=csv,noheader,nounits 2>/dev/null | head -1 | tr -d ' '
}
if [ "$DEVICE" = "gpu" ] && [ "$GPU_TEMP_MAX" != "0" ]; then
  T=$(gpu_temp)
  if [ -n "$T" ] && [ "$T" -ge "$GPU_TEMP_MAX" ]; then
    echo "GPU is at ${T}C (limit ${GPU_TEMP_MAX}C); let it cool first." >&2; exit 1
  fi
  echo "+ GPU at ${T:-unknown}C before start" >&2
fi
if ss -ltnH "sport = :$PORT" | grep -q .; then
  echo "port $PORT is already in use; stop the stale server first" >&2; exit 1
fi

mkdir -p "$TRACE_DIR" "$RES_DIR"
rm -f "$TRACE_DIR"/*.json* "$TRACE_DIR"/*.txt 2>/dev/null || true

echo "+ booting $TAG (eager=$EAGER, worker_in_engine=$WORKER_IN_ENGINE, profile=$PROFILE, kv_probe=$KV_PROBE); log: $LOG" >&2
if [ "$PROFILE" = "1" ]; then SERVE_PROFILE_DIR="$TRACE_DIR"; else SERVE_PROFILE_DIR=""; fi
DEVICE="$DEVICE" VARIANT="$VARIANT" ENFORCE_EAGER="$EAGER" PROFILE_DIR="$SERVE_PROFILE_DIR" \
  WORKER_IN_ENGINE="$WORKER_IN_ENGINE" EXTRA_SERVE_ARGS="$SCEN_ARGS" \
  setsid "$R/harness/serve.sh" > "$LOG" 2>&1 &
SERVER_PID=$!
PYSPY_PID=""
cleanup() {
  [ -n "$PYSPY_PID" ] && kill -INT "$PYSPY_PID" 2>/dev/null || true
  # serve.sh runs in its own session: signal the whole group (API server,
  # engine core, worker), not just the launcher.
  kill -TERM -- "-$SERVER_PID" 2>/dev/null || true
  for _ in $(seq 1 30); do kill -0 "$SERVER_PID" 2>/dev/null || break; sleep 1; done
  kill -KILL -- "-$SERVER_PID" 2>/dev/null || true
}
trap cleanup EXIT

echo -n "+ waiting for $TARGET/health " >&2
T_BOOT=$(date +%s.%N)
for _ in $(seq 1 600); do
  if curl -sf "$TARGET/health" >/dev/null 2>&1; then echo " up" >&2; break; fi
  if ! kill -0 "$SERVER_PID" 2>/dev/null; then
    echo; echo "server died; log $LOG:" >&2; tail -40 "$LOG" >&2; exit 1
  fi
  echo -n "." >&2; sleep 2
done
T_READY=$(date +%s.%N)

# Warm up outside the window: allocator, lazily imported code, first-shape setup.
"$PY" "$R/harness/drive_requests.py" --target "$TARGET" --model "$MODEL" \
  --requests 1 --max-tokens 8 --prompt-tokens 32 \
  --preamble "Warmup request; distinct prefix, not part of the measured prompt. " >/dev/null
curl -sf -X POST "$TARGET/reset_prefix_cache" >/dev/null || true

T_PROFILE=$(date +%s.%N)
if [ "$PROFILE" = "1" ]; then
  echo "+ start_profile (max_iterations=$ITERS)" >&2
  curl -sf -X POST "$TARGET/start_profile" -H 'Content-Type: application/json' \
    -d "{\"max_iterations\": $ITERS}" >/dev/null
fi

# On the CPU in default mode the scheduler lives in an engine-core process that
# has no torch profiler (the worker process has it). Sample it instead.
ENGINE_PID=$(grep -o '(EngineCore pid=[0-9]*)' "$LOG" | head -1 | grep -o '[0-9]*' || true)
PYSPY=$(command -v py-spy || echo "$R/.venv-loadgen/bin/py-spy")
T_PYSPY=""
if [ "$PROFILE" = "1" ] && [ "$DEVICE" = "cpu" ] && [ "$MODE" = "default" ] && [ -n "$ENGINE_PID" ] && [ -x "$PYSPY" ]; then
  T_PYSPY=$(date +%s.%N)
  "$PYSPY" record --pid "$ENGINE_PID" --format chrometrace --rate 500 --nonblocking --idle \
    --output "$TRACE_DIR/enginecore.pyspy.chrometrace.json" > "$TRACE_DIR/pyspy.log" 2>&1 &
  PYSPY_PID=$!
  echo "+ py-spy sampling engine core pid $ENGINE_PID" >&2
fi

"$PY" "$R/harness/drive_journey.py" --target "$TARGET" --model "$MODEL" \
  --scenario "$SCENARIO" --out "$RES_DIR/$TAG.client.json"

if [ "$PROFILE" = "1" ]; then
echo "+ stop_profile" >&2
curl -sf -X POST "$TARGET/stop_profile" >/dev/null || echo "WARN: stop_profile returned non-2xx" >&2
if [ -n "$PYSPY_PID" ]; then
  kill -INT "$PYSPY_PID" 2>/dev/null || true; wait "$PYSPY_PID" 2>/dev/null || true; PYSPY_PID=""
fi

# Trace files are written asynchronously (gzip of a large JSON); wait until the
# set of files and their sizes stop changing.
echo -n "+ waiting for traces to be flushed " >&2
prev=""; stable=0
fi
for _ in $(seq 1 120); do
  [ "$PROFILE" = "1" ] || break
  cur=$(ls -l "$TRACE_DIR"/*.pt.trace.json* 2>/dev/null | awk '{print $5, $9}' | sort | md5sum)
  n=$(ls "$TRACE_DIR"/*.pt.trace.json* 2>/dev/null | wc -l)
  if [ "$cur" = "$prev" ] && [ "$n" -ge 2 ]; then stable=$((stable + 1)); else stable=0; fi
  [ "$stable" -ge 3 ] && break
  prev=$cur; echo -n "." >&2; sleep 2
done
echo >&2

cleanup
trap - EXIT
[ "$DEVICE" = "gpu" ] && echo "+ GPU at $(gpu_temp)C after run" >&2

"$PY" - "$LOG" "$TRACE_DIR" "$RES_DIR/$TAG.manifest.json" <<PY
import glob, json, os, re, sys
log, trace_dir, out = sys.argv[1:4]
text = open(log, errors="replace").read()
roles = {}
for role, pid in re.findall(r"\((\w+) pid=(\d+)\)", text):
    roles.setdefault(role, int(pid))
facts = [l.strip()[:2000] for l in text.splitlines() if re.search(
    r"Initializing a V1 LLM engine|non-default args|[Aa]sync(hronous)? scheduling|"
    r"cudagraph|CUDA graph|[Cc]ompil|torch.compile|Graph capturing|KV cache|"
    r"Maximum concurrency|executor|attention backend|Using .* backend|OMP|threads", l)]
files = sorted(os.path.basename(p) for p in glob.glob(os.path.join(trace_dir, "*")))
m = {
    "tag": "$TAG", "device": "$DEVICE", "variant": "$VARIANT", "model": "$MODEL",
    "mode": "$MODE", "scenario": "$SCENARIO", "max_iterations": $ITERS,
    "enforce_eager": bool($EAGER), "worker_in_engine": bool($WORKER_IN_ENGINE),
    "extra_serve_args": "$SCEN_ARGS", "profiled": bool($PROFILE),
    "kv_probe": os.path.basename("$PROBE_OUT") or None,
    "t_boot": $T_BOOT, "t_ready": $T_READY, "t_start_profile": $T_PROFILE,
    "t_pyspy_start": float("${T_PYSPY:-0}") or None,
    "process_pids": roles, "trace_files": files, "log_facts": facts,
}
json.dump(m, open(out, "w"), indent=2)
print(f"manifest: {out}")
print("processes:", roles)
print("trace files:", *files, sep="\n  ")
PY
