# 05 — One request path, two devices

Two traces of the same engine, same weights, same prompt: one on the laptop CPU
(`float32`), one on the RTX 4060 (`bfloat16`). The question is not "which is
faster" — that is one number — but *what the work is made of on each device*, and
which parts of vLLM's request path change shape when the device changes.

```bash
DEVICE=cpu VARIANT=ref harness/profile_run.sh   # -> traces/cpu-ref-profile/rank0*
DEVICE=gpu VARIANT=ref harness/profile_run.sh   # -> traces/gpu-ref-profile/rank0*
analysis/compare_runs.py --cpu results/cpu-ref-profile-stages.csv \
                         --gpu results/gpu-ref-profile-stages.csv \
                         --out results/profile-compare.csv
analysis/plot_compare.py results/cpu-ref-profile-stages.csv \
                         results/gpu-ref-profile-stages.csv --out figures/profile
```

## What was captured

Both runs: Qwen3-0.6B (`ref`, no quantization), `--enforce-eager`, one process,
`record_shapes=True`, one warmup request, then four serial requests of a
512-token prompt with `ignore_eos` and 128 output tokens. The profiler records
the first 20 engine iterations, which is exactly **one prefill step (512 context
tokens) plus 19 decode steps** over 28 layers — identical on both legs, because
the prompt text, tokenizer and iteration cap are identical. Every table below is
the same 20 iterations.

The legs differ in dtype by design (CPU has no bf16 hardware, so `dtype_for`
gives each device its best precision), and therefore in weight bytes. That is
covered under caveats; it does not affect the *shape* of the work.

## Where the time goes

Attributed work, `results/profile-compare.csv`. CPU is host self time (on a CPU
the host *is* the device); GPU is summed kernel time.

| op class | cpu ms | cpu % | gpu ms | gpu % | cpu/gpu |
|---|---:|---:|---:|---:|---:|
| linear.gate_up | 1014.72 | 26.7 | 35.34 | 25.4 | 28.7× |
| linear.qkv | 731.69 | 19.3 | 23.94 | 17.2 | 30.6× |
| linear.down | 554.57 | 14.6 | 18.32 | 13.2 | 30.3× |
| linear.lm_head | 478.84 | 12.6 | 25.06 | 18.0 | 19.1× |
| linear.o_proj | 362.05 | 9.5 | 12.55 | 9.0 | 28.9× |
| attention.paged | 211.03 | 5.6 | 13.44 | 9.7 | 15.7× |
| elementwise | 309.81 | 8.2 | — | — | — |
| rope | 50.56 | 1.3 | 1.46 | 1.1 | 34.5× |
| attention.kv_write | 36.61 | 1.0 | 1.29 | 0.9 | 28.3× |
| activation | 27.55 | 0.7 | 1.26 | 0.9 | 21.9× |
| norm | — | — | 4.93 | 3.5 | — |
| python (plumbing) | 15.10 | 0.4 | 1.37 | 1.0 | 11.0× |
| sampler | 6.04 | 0.2 | — | — | — |
| **total** | **3798.57** | | **138.97** | | **27.3×** |

Stage totals, for orientation: `model.forward` 86.9% (CPU) vs 81.4% (GPU),
`model.lm_head` 12.6% vs 18.0%, everything else (prepare inputs, sampling,
engine glue) under 0.5% on CPU and under 0.3% on GPU.

### 1. The device changes the cost by 29×, not the shape of the work

The projection mix is almost the same on both: gate_up is the largest in both
(26.7% / 25.4%), then qkv, down, o_proj, and lm_head as a separate stage. The
per-projection ratios cluster tightly at 28.7–30.6×. That is what you expect if
the work is the same arithmetic and the device is simply a different multiplier:
vLLM dispatches the same four GEMMs per layer either way, and nothing about
scheduling or batching moved.

The one projection that escapes the cluster is lm_head at 19.1×, and the reason
is in its shape. It is the only GEMM whose output is the vocabulary (151,936
wide): on the GPU it is bandwidth-bound, because each token streams
`151936 × 1024 × 2 B ≈ 311 MB` of weights, while the projections stream
12–25 MB each. The CPU's fp32 weights are twice the bytes, but its GEMMs are
compute-limited rather than bandwidth-limited, so it loses proportionally less
on this one.

### 2. Decode is a weight-streaming problem; prefill is not

Per token, from the step report and per-step device sums. CPU rows are host self
time; GPU rows are kernel time, and the GPU's prefill step has a 18.2 ms host
span for the same 24.1 ms of device work because the device runs behind the
launcher:

| | CPU | GPU |
|---|---:|---:|
| prefill forward (512 tokens) | 1386.2 ms | 24.1 ms |
| prefill per token | 2.71 ms | 0.047 ms |
| decode forward (1 token) | 116.6 ms | 6.05 ms |
| prefill / decode cost per token | 43× cheaper | 129× cheaper |

Both devices show the same structural fact: decoding one token costs *far* more
than processing one prefill token. Prefill runs 512 tokens through each weight
matrix in one GEMM, so every weight byte is reused 512 times; decode runs one
token, so the whole model is streamed from memory to multiply each weight once.
For this model (~0.6B params with tied embeddings) that is ~1.2 GB per decode
token in bf16, ~2.4 GB in fp32.

The measured decode times are consistent with that: 1.2 GB / 6.05 ms ≈ 197 GB/s
on the GPU (against ~272 GB/s peak on this part), and 2.4 GB / 116.6 ms ≈ 21 GB/s
on the CPU. A compute-bound explanation would predict the opposite ordering of
prefill/decode, so the prefill→decode asymmetry is the device-independent story
here, and it is the reason decode latency dominates single-stream serving.

### 3. Attention gets a bigger *share* on the GPU by getting smaller

`attention.paged` is 9.7% of GPU work and 5.6% of CPU work, which reads as "the
GPU spends more on attention". It does not: 13.44 ms against 211.03 ms, a 15.7×
gain — the *smallest* ratio in the table. The share rises because everything
else shrank by ~29× while attention shrank by ~16×, so attention's slice grew.
Per layer it is 7.3% of CPU decode time and 3.5% of GPU decode time.

This is the general trap with share tables on two devices, and the reason
`compare_runs.py` prints both columns.

### 4. RMSNorm exists on the GPU and is *arithmetic* on the CPU

There is no `norm` row for the CPU at all, and 8.2% of CPU time sits in
`elementwise` (90,960 ATen ops: `aten::mul`, `aten::pow`, `aten::sum`,
`aten::div_`, `aten::add`, plus view/slice/copy plumbing). The GPU has fused
`_C::rms_norm` and `_C::fused_add_rms_norm` kernels — 2,260 of them, 3.5%.

The CPU path has no fused RMSNorm op, so the layer's normalization arrives as a
chain of elementwise calls, and the equally unfused reshapes around attention add
to the same bucket. Same math, different decomposition: the GPU's 3.5% and the
CPU's 8.2% are not comparable as "how expensive is normalization", only as "how
much of each device's time lands in that bucket".

### 5. Frontend and engine glue are noise on both — but not the same noise

Engine-core glue (sample + schedule + postproc) is 1.45 ms/step on the CPU and
0.64 ms/step on the GPU. Its *share* is higher on the GPU (3.1% vs 0.7%) because
the model forward got so much cheaper; in absolute terms the GPU is still the
faster engine core. Neither device is anywhere near frontend-bound: prompt
handling and detokenization do not appear in the traced window at all.

### 6. First-step, per-shape cost is real on both, and much bigger on the GPU

Layer 0 of the first decode step versus the other 27 layers:

| | L0 | other layers | ratio |
|---|---:|---:|---:|
| CPU | 5.41 ms | 4.12 ms | 1.31× |
| GPU | 6.64 ms | 0.66 ms | 10.09× |

By step 19 the gap is gone on the CPU (0.93×) and down to 1.22× on the GPU. This
is one-time per-shape setup at the new `M=1` shape — on the CPU a oneDNN
primitive is built, on the GPU the first launch of these kernels happens — and it
is why `step_report.py` excludes L0 from the layer means and reports it
separately instead of letting it inflate the average.

On the GPU it is also the difference between "28 layers cost 0.66 ms each" and
"the first one costs ten times that", which matters if you are looking at
time-to-first-token rather than steady-state throughput.

## Caveats that travel with these numbers

- **Different metrics.** CPU self time and GPU kernel time both answer "what did
  this op cost on this device", but they are not the same physical measurement. A
  GPU kernel's time is device time; the CPU's is host time. Do not read the
  total ratio (27.3×) as a serving-throughput ratio — the GPU leg is eager, has
  no CUDA graphs (R6), and was driven by four serial requests.
- **Different dtypes by design.** fp32 vs bf16 means the GPU reads and multiplies
  half the bytes. The 29× projection ratio is arithmetic *and* precision, not
  silicon alone.
- **Profiled windows are distorted.** Python self time in the window is 27.9 s
  (CPU) and 2.1 s (GPU) against 3.8 s / 0.14 s of attributed work: the profiler's
  own recording overhead and asyncio waits live there. That is why the taxonomy
  attributes work and prints the excluded share rather than hiding it.
- **Absolute CPU numbers drift between runs.** A repeat of the same CPU run gave
  4.12 ms/layer here versus ~3.6 ms/layer earlier (≈15%), with the projection
  shares moving under 1.5 points. Compare shares and per-projection ratios;
  treat absolute CPU milliseconds as ±15%.
- **The GPU window is one request.** `max_iterations=20` stops at the end of
  request 0's 19th decode step; the remaining three requests are served but not
  recorded. That is sufficient for the per-step anatomy, which repeats layer for
  layer, but it is not a throughput sample.
- **The 32-token prefix-cache trap.** The harness warms up with a short request
  and resets the prefix cache. On the GPU the reset did not take effect and the
  profiled prefill scheduled 480 tokens instead of 512 — two 16-token blocks came
  from the warmup. The warmup now carries a unique preamble, and
  `profile_run.sh` reads the scheduler's own context-token count back from the
  trace, so a mismatch fails loudly instead of skewing the comparison.

## Files

| what | where |
|---|---|
| per-run stage CSVs | `results/cpu-ref-profile-stages.csv`, `results/gpu-ref-profile-stages.csv` |
| merged comparison | `results/profile-compare.csv` |
| stage / op-class / projection figures | `figures/profile/fig{1,2,3}-*.png` |
| per-step anatomy figures | `figures/fig-step-cpu-ref.png`, `figures/fig-step-gpu-ref.png` |
| traces (not committed) | `traces/{cpu,gpu}-ref-profile/rank0*.json.gz` |
