#!/usr/bin/env bash
# Capture everything that makes this measurement reproducible.
# Usage: harness/collect_env.sh [> results/env-<host>.txt]
set -u
source "$(dirname "${BASH_SOURCE[0]}")/env.sh"

# Record the interpreter that actually serves, not whatever is on PATH:
# a stale vllm in the user site-packages would otherwise be reported.
PY=${PY:-$HOME/workspace/vllm/.venv/bin/python}
[ -x "$PY" ] || PY=$(command -v python || echo python)
echo "=== interpreter ==="
echo "PY=$PY"
"$PY" -c "import sys; print('python', sys.version.split()[0], sys.executable)" 2>/dev/null || echo "python: not found"

echo "=== study pins ==="
echo "vllm_commit_target=$VLLM_COMMIT"
echo "vllm_tag=$VLLM_TAG"
echo "guidellm_pin=$GUIDELLM_PIN"
echo "date=$(date -Is)"

echo
echo "=== host ==="
uname -a
echo "hostname=$(hostname)"
echo "nproc=$(nproc)"
free -g | head -2

echo
echo "=== cpu ==="
grep -m1 "model name" /proc/cpuinfo || true
grep -m1 "^flags" /proc/cpuinfo | tr ' ' '\n' \
  | grep -E "^(avx|avx2|avx512f|avx512_bf16|avx512_vnni|avx_vnni|amx_tile|amx_bf16|amx_int8|f16c|sse4_2)$" \
  | sort | tr '\n' ' ' || true
echo
echo "note: amx_* and avx512_* absent => no AMX/AVX-512; bf16 is emulated on such hosts"
echo "gcc_isa_target=$(gcc -march=native -Q --help=target 2>/dev/null | grep -E '^\s+-march=' | awk '{print $2}')"
echo "build_flags=${CXXFLAGS:-<unset>}  (CPU leg builds with -march=native: host-specific binary)"

echo
echo "=== accelerators ==="
if command -v nvidia-smi >/dev/null 2>&1; then
  nvidia-smi --query-gpu=name,memory.total,compute_cap,driver_version \
             --format=csv,noheader || true
else
  echo "no nvidia-smi (CPU-only host)"
fi

echo
echo "=== python stack ==="
"$PY" -c "import sys; print('python', sys.version.split()[0], sys.executable)" 2>/dev/null || echo "python: not found"
"$PY" -c "import torch; print('torch', torch.__version__, 'cuda', torch.version.cuda)" 2>/dev/null || true

echo
echo "=== vllm identity (must be built from the pinned commit) ==="
if command -v vllm >/dev/null 2>&1; then
  "$PY" -c "import vllm; print('vllm', vllm.__version__, vllm.__file__)" 2>/dev/null || true
  SRC=$("$PY" -c "import vllm,os;print(os.path.dirname(os.path.dirname(vllm.__file__)))" 2>/dev/null)
  if [ -n "${SRC:-}" ] && [ -d "$SRC/.git" ]; then
    echo "vllm_source=$SRC"
    echo "vllm_source_head=$(git -C "$SRC" rev-parse HEAD 2>/dev/null)"
    echo "vllm_source_dirty=$(git -C "$SRC" status --porcelain 2>/dev/null | wc -l) files"
  fi
  "$PY" -c "from vllm.platforms import current_platform as p; print('platform', p.device_name, p.device_type)" 2>/dev/null || true
  "$PY" - <<'PYEOF' 2>/dev/null || true
import glob, os
try:
    import vllm._cpu_ops as ops
    d = os.path.dirname(ops.__file__)
    print("extension_variants=" + ",".join(sorted(os.path.basename(p) for p in glob.glob(os.path.join(d, "_C*.so")))))
except Exception as e:
    print("extension_variants=<unavailable:", e, ">")
PYEOF
fi
command -v vllm >/dev/null 2>&1 && vllm --version 2>/dev/null || true

echo
echo "=== cpu-leg thread and cache knobs ==="
echo "OMP_NUM_THREADS=$OMP_NUM_THREADS"
echo "VLLM_CPU_OMP_THREADS_BIND=$VLLM_CPU_OMP_THREADS_BIND"
echo "VLLM_CPU_KVCACHE_SPACE=${VLLM_CPU_KVCACHE_SPACE}GiB"

echo "=== runner selection (must match across legs) ==="
echo "VLLM_USE_V2_MODEL_RUNNER=${VLLM_USE_V2_MODEL_RUNNER:-<auto>}  (0=V1 pinned for both legs)"

echo "=== guidellm ==="
guidellm --version 2>/dev/null || echo "guidellm: not installed (pip install '$GUIDELLM_PIN')"

echo
echo "=== build prerequisites (CPU leg) ==="
echo "numactl-devel present: $([ -f /usr/include/numa.h ] && echo yes || echo NO - build will fail)"
