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

cd "$VLLM_SRC"

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
"$PY" -c "from vllm.platforms import current_platform as p; print('platform', p.device_name, p.device_type)"
