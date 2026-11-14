#!/usr/bin/env bash
# End-to-end CPU profiling runbook for Qwen3-0.6B on the vLLM CPU backend.
set -u
cd /home/akamra/workspace/vllm || exit 1
PY=/home/akamra/workspace/vllm/.venv/bin/python
D=/tmp/opencode/vllm-prof
M=Qwen/Qwen3-0.6B
export TOKENIZERS_PARALLELISM=false
export OMP_NUM_THREADS=${OMP_NUM_THREADS:-14}

step() { echo; echo "################ $* ################"; }

step "1. baseline timing (no instrumentation)"
$PY $D/measure.py --model $M --prose $D/prose.txt || exit 1

step "2. torch.profiler traces"
mkdir -p $D/traces
$PY $D/profile_torch.py --model $M --phase decode  --prose $D/prose.txt || exit 1
$PY $D/profile_torch.py --model $M --phase prefill --prose $D/prose.txt || exit 1
ls -la $D/traces

step "3. analyse traces"
for t in $D/traces/*.json.gz; do
  $PY $D/analyze_trace.py "$t" | tee "$(dirname "$t")/$(basename "$t" .json.gz).summary.txt"
done

step "4. py-spy flamegraph (decode)"
PYS=$(command -v py-spy || echo /home/akamra/.local/share/bin/py-spy)
$PYS record -f speedscope -r 200 -d 25 -o $D/decode.speedscope.json -- \
  $PY $D/bench_loop.py --model $M --mode decode --seconds 20 || echo "py-spy failed"

step "5. perf record (native hotspots, decode)"
perf record -F 499 -g -o $D/perf-decode.data -- \
  $PY $D/bench_loop.py --model $M --mode decode --seconds 15 || echo "perf record failed"
perf report --stdio --no-children --sort dso,symbol -i $D/perf-decode.data 2>/dev/null | head -70

step "DONE"
