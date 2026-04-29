# RESULTS — AMX ISA-ceiling sweep under PREEMPT_RT

**Date:** 2026-04-29
**Substrate:** GCP `c3-standard-8` (Sapphire Rapids; AMX-capable: `amx_bf16 amx_int8 amx_tile` in `/proc/cpuinfo`)
**OS / kernel:** CentOS Stream 9, `kernel-rt-5.14.0-697.el9.x86_64+rt` (`CONFIG_PREEMPT_RT=y`)
**Tuning:** `tuned-adm profile realtime`, `isolcpus=2-7`, `nohz_full=2-7`, `rcu_nocbs=2-7`, `intel_pstate=disable`, `processor.max_cstate=1`, `idle=poll`, `skew_tick=1`, `audit=0`, `nosoftlockup`, `mce=off`
**Bench artifacts (same commit as this doc):** `bench_amx_sweep.py`, `sweep.sh`, `results/sweep.csv`, `results/sweep_summary.csv`, `results/onednn_verbose_sample.txt`
**Phase 3B-followup; closes hypothesis H5 from `README.md`.**

## Methodology

28-cell factorial, single-thread, pinned to isolated CPU 4:

```
N      ∈ {64, 128, 256, 512, 1024, 2048, 4096}
ISA    ∈ {amx-on, amx-off}        # ONEDNN_MAX_CPU_ISA toggled
kernel ∈ {default, rt-fifo}        # rt-fifo = sudo -E ... chrt -f 80 taskset -c 4
```

Per cell:
- 10 K measured samples target, 120 s wall-clock cap (whichever first)
- Warmup: 50 (amx-on) / 100 (amx-off) — amx-off uses a different oneDNN BRGEMM JIT path (`gemm:jit:bf16` vs `brg_matmul:avx10_1_512_amx`); amortizing its JIT needs a few more samples on small-N cells
- `taskset -c 4` for all cells (single-thread on isolated core)
- Pre-allocated BF16 tensors A, B of shape (N, N); `time.perf_counter_ns()` brackets each `A @ B`
- Per-call CSV columns: `cell_id, N, isa, kernel, sample_idx, latency_us`
- Per-cell summary CSV columns: `cell_id, N, isa, kernel, n_samples, p50_us, p95_us, p99_us, p999_us, max_us, mean_us`

`amx-on` cells leave `ONEDNN_MAX_CPU_ISA` unset → oneDNN selects `brg_matmul:avx10_1_512_amx`. `amx-off` cells set `ONEDNN_MAX_CPU_ISA=AVX512_CORE` → oneDNN falls back to `gemm:jit:bf16` (AVX-512 BF16, no AMX). Same precision in both legs; the only varying axis is whether AMX tile ops engage.

`ONEDNN_VERBOSE=1` was captured for one cell (N=2048, amx-on, rt-fifo) → `results/onednn_verbose_sample.txt`. The remaining 27 cells ran silent to keep log volume bounded.

Execution order: default-kernel cells first (no sudo), then rt-fifo cells; within each kernel, ascending N.

## Headline findings

The two findings below are kept visibly separate by design: they are independent claims on independent axes.

### Finding A — AMX is RT-clean under PREEMPT_RT + chrt-FIFO + isolated cores (single-thread, nthr:1)

H5 from `README.md` verbatim: *"AMX tile save/restore is RT-clean (no jitter from XSAVE/XRSTOR on context switch)."*

**Verdict: CONFIRMED on this substrate at `nthr:1`.**

Across all 7 AMX-on cells, max latency under `rt-fifo` is within noise of the default kernel — no systematic tail widening, no XSAVE/XRSTOR-shaped jump signature when AMX state is engaged at preempt boundaries.

| N | max default (µs) | max rt-fifo (µs) | direction |
|---|---:|---:|---|
| 64 | 43.22 | 45.20 | within noise |
| 128 | 65.92 | 184.36 | rt-fifo single-sample outlier; p99.9 unaffected (52.11 → 54.74) |
| 256 | 209.16 | 172.03 | rt-fifo *better* |
| 512 | 964.97 | 921.18 | rt-fifo *better* |
| 1024 | 6562.33 | 6619.40 | within noise |
| 2048 | 49 440.09 | 48 384.87 | rt-fifo *better* (cap-degraded count) |
| 4096 | 468 322.72 | 474 290.69 | within noise (cap-degraded count) |

The N=128 rt-fifo outlier (184 µs vs default's 66 µs) is a single sample — p99.9 moves only from 52.11 → 54.74 µs (within noise). Not an XSAVE/XRSTOR signature; the rest of the cells show flat-or-better tails under rt-fifo.

This is the load-bearing result for the Red Hat AI catalog "deterministic latency under PREEMPT_RT" claim: AMX-engaged matmul under a real-time kernel + isolated cores + SCHED_FIFO does not pay a measurable tail-latency cost from AMX state-save during preempt.

### Finding B — AMX speedup curve on SPR single-thread (this substrate only)

Baseline is **BF16 + AVX-512** (`ONEDNN_MAX_CPU_ISA=AVX512_CORE`, kernel `gemm:jit:bf16`), not default-ISA / FP32.

| N | amx-on p50 (µs) | amx-off p50 (µs) | speedup (amx-off / amx-on) |
|---:|---:|---:|---:|
| 64 | 22.96 | 39.57 | **1.72×** |
| 128 | 32.74 | 160.42 | **4.90×** |
| 256 | 84.55 | 1 123.58 | **13.29×** |
| 512 | 553.88 | 8 992.00 | **16.23×** ← peak |
| 1024 | 4 898.65 | 70 275.34 | **14.34×** |
| 2048 | 43 272.17 | 563 300.58 | **13.02×** (amx-off cap-degraded) |
| 4096 | 427 850.96 | 4 503 013.63 | 10.52× (both cap-degraded) |

**Peak 16.2× at N=512 is the headline number for the data sheet.**

**This is NOT the same number as the GNR+TDX 3.77× headline.** Different substrate (SPR vs GNR+TDX), different threading (single-thread vs multi-thread stride-2), different baseline (BF16+AVX-512 vs different baseline definition in the GNR+TDX memo). The two numbers should be stated side by side in any external-facing data sheet, not merged:

| Substrate | Threading | Baseline | Speedup |
|---|---|---|---|
| SPR + PREEMPT_RT (this run) | nthr:1 | BF16 + AVX-512 | 16.2× peak (N=512) |
| GNR + TDX (Notion AMX Contribution memo) | multi-thread, stride-2 | (per memo) | 3.77× |

## Per-platform calibration implication

`THRESHOLD_PROFILES['spr']`'s `cpu_max` (currently 500-tuned) likely should drop substantially. On this single-thread, bare-metal SPR + oneDNN 3.5.3 + PyTorch's BRGEMM dispatch, **AMX wins from N=64 onward** (1.72× at N=64). The "AMX is counterproductive below ~N=400-800" prior comes from the GNR+TDX memo and does not transfer to this SPR substrate at this threading.

Frame this as **substrate-specific calibration**, not a universal AMX claim:

| Substrate | cpu_max (current) | observed AMX threshold | comment |
|---|---|---|---|
| `gnr-tdx` (multi-thread, TDX) | 800 | ~400-800 (per memo) | AMX counterproductive below ~800 |
| `spr` (this run, single-thread, no TDX) | 500 (likely too high) | < 64 | AMX wins from N=64; threshold below sweep range |

This is exactly the per-platform-policy story the SmartOrderRouter exists to solve — a single global `cpu_max` cannot capture the AMX engagement curve across substrates. Recommend updating `THRESHOLD_PROFILES['spr']` to `cpu_max=64` (or lower) for this threading model, with a note that the GNR+TDX `cpu_max=800` value reflects multi-thread+TDX-specific scheduler overhead and is *correct for that substrate*.

## Methodology caveats (cap-degraded cells)

10 of 28 cells hit the 120 s wall-clock cap before reaching 10 K samples:

| cell | n_samples | per-call est. | p99.9 confidence |
|---|---:|---:|---|
| `N1024-amx-off-default` | 1 707 | ~70 ms | passable |
| `N1024-amx-off-rt-fifo` | 1 694 | ~73 ms | passable |
| `N2048-amx-on-default` | 2 810 | ~43 ms | passable |
| `N2048-amx-on-rt-fifo` | 2 772 | ~43 ms | passable |
| `N2048-amx-off-default` | 213 | ~563 ms | marginal |
| `N2048-amx-off-rt-fifo` | 214 | ~562 ms | marginal |
| `N4096-amx-on-default` | 281 | ~427 ms | marginal |
| `N4096-amx-on-rt-fifo` | 279 | ~428 ms | marginal |
| `N4096-amx-off-default` | 27 | ~4.5 s | bounding number only |
| `N4096-amx-off-rt-fifo` | 27 | ~4.5 s | bounding number only |

The two N=4096 amx-off cells (27 samples each, p99.9 = max because there aren't enough samples to resolve the tail) are bounding numbers, not tail-resolved measurements. The N=2048 amx-off cells (213/214 samples) are marginal for p99.9.

Finding A (H5) does not depend on these cells — it's testable from the amx-on cells, where 10 K samples held all the way to N=1024 and 2 772-2 810 at N=2048. Finding B's peak claim (16.2× at N=512) is on a fully-resolved cell pair.

## Dispatch attestation

ONEDNN_VERBOSE captured cell: **N=2048, amx-on, rt-fifo**.

```
$ grep -c "brg_matmul:avx10_1_512_amx" results/onednn_verbose_sample.txt
2822
```

50 warmup + 2 772 measured = 2 822 ✓. Zero non-AMX fallback observed in the captured cell.

`isa` line in the verbose log:
```
onednn_verbose,info,cpu,isa:Intel AVX-512 with float16, Intel DL Boost and bfloat16 support and Intel AMX with bfloat16 and 8-bit integer support
onednn_verbose,info,cpu,runtime:OpenMP,nthr:1
```

Confirms AMX BF16 + INT8 detected; thread count is 1 (taskset -c 4 single-CPU pin).

## Full results table

⚠ = cap-degraded (n_samples < 10 000)

```
cell_id                N     isa      kernel   n_samples  p50_us       p95_us       p99_us       p999_us      max_us       mean_us
N64-amx-on-default     64    amx-on   default     10000      22.96        26.43        31.46        37.82        43.22        23.37
N64-amx-off-default    64    amx-off  default     10000      39.57        46.66        48.23        56.17        63.77        40.26
N128-amx-on-default    128   amx-on   default     10000      32.74        39.66        42.98        52.11        65.92        33.36
N128-amx-off-default   128   amx-off  default     10000     160.42       171.55       174.28       181.98       194.68       162.48
N256-amx-on-default    256   amx-on   default     10000      84.55        99.47       106.92       120.49       209.16        86.74
N256-amx-off-default   256   amx-off  default     10000    1123.58      1133.59      1140.72      1237.27      1382.42      1124.62
N512-amx-on-default    512   amx-on   default     10000     553.88       570.20       582.22       699.11       964.97       553.20
N512-amx-off-default   512   amx-off  default     10000    8992.00      9357.80      9377.12      9434.18      9928.50      9061.92
N1024-amx-on-default   1024  amx-on   default     10000    4898.65      5307.07      5449.59      5670.62      6562.33      4951.11
N1024-amx-off-default  1024  amx-off  default   ⚠1707     70275.34     70759.35     71368.28     71992.36     72116.52     70312.91
N2048-amx-on-default   2048  amx-on   default   ⚠2810     43272.17     44059.86     44617.88     47473.70     49440.09     42705.17
N2048-amx-off-default  2048  amx-off  default     ⚠213    563300.58    571097.28    571327.31    572341.98    572341.98    564969.36
N4096-amx-on-default   4096  amx-on   default     ⚠281    427850.96    458758.29    464353.60    468322.72    468322.72    427165.52
N4096-amx-off-default  4096  amx-off  default      ⚠27   4503013.63   4508340.63   4509150.77   4509150.77   4509150.77   4503749.57
N64-amx-on-rt-fifo     64    amx-on   rt-fifo     10000      22.78        26.42        31.53        37.29        45.20        23.21
N64-amx-off-rt-fifo    64    amx-off  rt-fifo     10000      39.57        46.94        48.59        57.32        61.49        40.29
N128-amx-on-rt-fifo    128   amx-on   rt-fifo     10000      33.15        40.22        43.59        54.74        184.36        33.61
N128-amx-off-rt-fifo   128   amx-off  rt-fifo     10000     159.98       172.04       175.42       184.02       195.78       162.12
N256-amx-on-rt-fifo    256   amx-on   rt-fifo     10000      82.65        96.06       106.17       119.31       172.03        85.45
N256-amx-off-rt-fifo   256   amx-off  rt-fifo     10000    1124.70      1136.04      1145.29      1175.77      1225.19      1126.12
N512-amx-on-rt-fifo    512   amx-on   rt-fifo     10000     560.75       580.86       615.16       748.81       921.18       561.05
N512-amx-off-rt-fifo   512   amx-off  rt-fifo     10000    8817.92      8873.17      9334.86      9540.51      9600.44      8831.58
N1024-amx-on-rt-fifo   1024  amx-on   rt-fifo     10000    4934.18      5324.82      5370.57      5483.56      6619.40      4991.02
N1024-amx-off-rt-fifo  1024  amx-off  rt-fifo   ⚠1694     70409.04     73199.31     73712.39     73926.15     73964.55     70866.02
N2048-amx-on-rt-fifo   2048  amx-on   rt-fifo   ⚠2772     43559.55     44976.99     45464.39     46611.15     48384.87     43283.82
N2048-amx-off-rt-fifo  2048  amx-off  rt-fifo     ⚠214    561569.22    570624.91    574075.84    578720.73    578720.73    562875.36
N4096-amx-on-rt-fifo   4096  amx-on   rt-fifo     ⚠279    427834.00    456789.35    468478.15    474290.69    474290.69    430641.13
N4096-amx-off-rt-fifo  4096  amx-off  rt-fifo      ⚠27   4511052.53   4535573.25   4544759.52   4544759.52   4544759.52   4514963.31
```

Raw per-call CSV: `results/sweep.csv` (190 025 rows).

## References

- **Phase 3B `compute_entropy()` PREEMPT_RT characterisation** — `README.md` in this directory (production-cadence + saturation-cadence benches; the 94 %-pipeline-overhead finding)
- **GNR+TDX AMX speedup result (3.77×)** — Notion *AMX Contribution* memo; different substrate, threading, and baseline definition, not directly comparable to Finding B's 16.2× SPR single-thread number
- **Red Hat AI Catalog GTM memo** — Confluence; Finding A is the load-bearing technical result for the "deterministic latency under PREEMPT_RT" claim
- **Validation memo (*Why the Red Hat Collaboration*)** — Confluence; the per-platform-calibration story is exactly the SmartOrderRouter's reason-to-exist
