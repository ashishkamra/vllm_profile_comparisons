#!/usr/bin/env bash
# Prove which instruction sets the CPU leg actually runs, instead of trusting the
# build flags.
#
#   harness/probe_isa.sh > results/isa-$(hostname).txt
#
# Two different questions, two different answers:
#   1. Which extension variants exist and which one vLLM dispatches to.
#   2. Which ISA oneDNN picks at *runtime* for the int8 GEMM (the hot path on this
#      host, since AMX is absent and vLLM falls back to oneDNN). oneDNN chooses
#      from CPUID at runtime, so the compiled objects alone prove nothing.
set -eu
source "$(dirname "${BASH_SOURCE[0]}")/env.sh"

echo "=== host ==="
hostname
grep -m1 "model name" /proc/cpuinfo
lscpu | grep -E "^(CPU\(s\)|Thread|Core|Socket)"

echo
echo "=== extension variants on disk ==="
ls -l "$VLLM_SRC"/vllm/_C*.so 2>/dev/null | awk '{print $5, $9}'

echo
echo "=== dispatched variant (what an import gets) ==="
"$PY" - <<'PYEOF'
import glob, os
import torch, vllm
from vllm.platforms import current_platform
current_platform.import_kernels()
d = os.path.dirname(vllm.__file__)
print("avx512_supported:", torch.cpu._is_avx512_supported())
print("avx512_bf16_supported:", torch.cpu._is_avx512_bf16_supported())
print("amx_tile_supported:", torch.cpu._is_amx_tile_supported())
# The import raises ImportError on purpose (no PyInit__C_AVX2) and the module is
# dropped from sys.modules, so ask the loader what is actually mapped instead.
pkg = os.path.dirname(vllm.__file__) + os.sep
mapped = sorted({
    os.path.basename(line.split()[-1])
    for line in open("/proc/self/maps")
    if line.split()[-1].startswith(pkg) and "/_C" in line
})
print("mapped_extension_libs:", ", ".join(mapped))
PYEOF

echo
echo "=== instructions actually emitted in the dispatched .so ==="
# -march=native enables these; a stock -mavx2 -mf16c build would not emit them.
SO="$VLLM_SRC/vllm/_C_AVX2.abi3.so"
if [ -f "$SO" ]; then
  for mnemonic in bmi2 shlx sarx rorx fma avx512 zmm; do
    case "$mnemonic" in
      bmi2)    pat='\b(shlx|sarx|rorx)\b' ;;
      fma)     pat='\bvfmadd[0-9]*' ;;
      avx512)  pat='\bv(pternlog|compress)' ;;
      zmm)     pat='%zmm' ;;
      *)       pat="\\b$mnemonic\\b" ;;
    esac
    n=$(objdump -d "$SO" 2>/dev/null | grep -cE "$pat" || true)
    printf '  %-8s %s\n' "$mnemonic" "$n"
  done
fi

echo
echo "=== runtime ISA chosen by oneDNN for the int8 GEMM ==="
# The int8 linear path on a non-AMX CPU is oneDNN (docs/02-methodology.md).
# DNNL_VERBOSE prints the selected ISA level and the kernel implementation.
DNNL_VERBOSE=1 "$PY" - <<'PYEOF' 2>&1 | grep -E "isa:|matmul|^probe"
import torch
import vllm
from vllm.platforms import current_platform
from vllm import _custom_ops as ops

current_platform.import_kernels()

M, K, N = 64, 1024, 4096
w = torch.randn(N, K).to(torch.int8).t()   # oneDNN wants [K, N], column-major
w_scales = torch.rand(N, 1)
h = ops.create_onednn_scaled_mm(w, w_scales, torch.float32, True, False)
a = torch.randn(M, K)
aq, ascale, azp = ops.onednn_scaled_int8_quant(a)
out = torch.empty(M, N, dtype=torch.float32)
ops.onednn_scaled_mm(h, aq, out, ascale, azp, None, None)
print("probe: oneDNN gemm ran", tuple(out.shape))
PYEOF
