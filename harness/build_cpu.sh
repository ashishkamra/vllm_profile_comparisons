#!/usr/bin/env bash
# Build vLLM from source for the CPU leg. There are no CPU wheels, so the CPU leg
# always builds from the pinned commit.
#
#   VLLM_SRC=~/workspace/vllm harness/build_cpu.sh
#
# Prerequisites (documented by vLLM: docs/getting_started/installation/cpu.x86.inc.md)
#   Fedora/RHEL: sudo dnf install -y numactl-devel
#   Debian/Ubuntu: sudo apt-get install -y libnuma-dev
# Without numa.h the build fails at csrc/cpu/utils.cpp with "fatal error: numa.h".
set -eu
source "$(dirname "${BASH_SOURCE[0]}")/env.sh"

VLLM_SRC=${VLLM_SRC:-$HOME/workspace/vllm}
PY=${PY:-$VLLM_SRC/.venv/bin/python}
export VLLM_TARGET_DEVICE=cpu
export MAX_JOBS=${MAX_JOBS:-$(nproc)}
export CMAKE_BUILD_PARALLEL_LEVEL=$MAX_JOBS

# Build with every ISA extension the build host has. This makes the wheel
# host-specific: it must only be run on the machine that built it. Record the
# expansion (results/env-*.txt) because it changes the kernels under test.
export CXXFLAGS="${CXXFLAGS:--march=native}"
export CFLAGS="${CFLAGS:--march=native}"

cd "$VLLM_SRC"

echo "### build-host ISA"
gcc ${CXXFLAGS} -Q --help=target 2>/dev/null | grep -E "^\s+-march=|^\s+-mtune=" || true
gcc ${CXXFLAGS} -Q --help=target 2>/dev/null \
  | grep -E "\-mavx2\b|\-mavxvnni|\-mavx512f|\-mamx-tile|\-mfma\b" || true

if [ ! -f /usr/include/numa.h ]; then
  echo "ERROR: /usr/include/numa.h missing. Install numactl-devel (Fedora) or" >&2
  echo "       libnuma-dev (Debian) first; the CPU extension will not compile." >&2
  exit 1
fi

HEAD=$(git rev-parse HEAD)
if [ "$HEAD" != "$VLLM_COMMIT" ]; then
  echo "WARNING: source is at $HEAD, study pins $VLLM_COMMIT ($VLLM_TAG)" >&2
  echo "         results are not comparable across commits. Ctrl-C to abort." >&2
  sleep 5
fi

# cmake caches compile flags in the build tree, so a flag change (or a version
# switch) needs a fresh configure or the old flags silently win.
rm -rf "$VLLM_SRC/.deps" "$VLLM_SRC/build"

echo "### venv (python 3.12: the version CI builds and the wheels target)"
uv venv --python 3.12 "$VLLM_SRC/.venv"

echo "### build deps"
uv pip install --python "$PY" \
  "cmake>=3.26.1" ninja "packaging>=24.2" "setuptools>=77.0.3,<81.0.0" \
  setuptools-scm "setuptools-rust>=1.9.0" wheel jinja2 "torch==2.14.0" \
  --torch-backend=cpu

echo "### editable install (compiles csrc for the CPU target: 10-40 min)"
uv pip install --python "$PY" -e . --no-build-isolation --torch-backend=cpu

echo "### verify"
"$PY" -c "import vllm; print('vllm', vllm.__version__)"
# v0.33 names the AVX-512/AMX variant "_C" and the AVX2 one "_C_AVX2", then picks
# at import time in platforms/cpu.py. Importing vllm._C directly on a non-AVX512
# host is an illegal instruction by design, so check the *dispatched* path.
"$PY" - <<'PYEOF'
import glob, os
import torch
import vllm
from vllm.platforms import current_platform

current_platform.import_kernels()
d = os.path.dirname(vllm.__file__)
print("extension variants:", sorted(os.path.basename(p) for p in glob.glob(os.path.join(d, "_C*.so"))))
print("avx512_supported:", torch.cpu._is_avx512_supported(),
      "-> dispatches to", "_C" if torch.cpu._is_avx512_supported() else "_C_AVX2")
print("cpu_attn op registered:", hasattr(torch.ops._C, "cpu_attention_with_kv_cache"))
PYEOF
"$PY" -c "from vllm.platforms import current_platform as p; print('platform', p.device_name, p.device_type)"
