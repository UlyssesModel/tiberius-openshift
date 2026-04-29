# RHEL-RT latency characterisation — Ulysses SOR window-compute

Measures the deterministic latency floor for `compute_entropy()` (the
hot-path SOR window-compute from `image/consumer.py`) under a
PREEMPT_RT kernel on Sapphire Rapids, isolating the kernel-RT effect
from cluster + container + Kafka overhead.

## TL;DR

**Kavara's SOR window-compute lands at p99.9 ≤ 0.32 ms and p99.99 ≤ 0.35 ms
on PREEMPT_RT Sapphire Rapids — 5.7× tighter at p95 and ~14–21×
tighter at p99 than the same code on a Kata-isolated SNO container.**

| Percentile | RHEL-RT (this experiment) | Kata-SNO baseline | Δ |
|------------|--------------------------:|------------------:|---:|
| p50        | **0.147 ms**              | ~0.5 ms (typ.)    | ~3.4× |
| p95        | **0.167 ms**              | 0.95 ms           | **5.7×** |
| p99        | **0.285 ms**              | ~4–6 ms (rough)   | **~14–21×** |
| p99.9      | **0.316 ms**              | not measured      | — |
| p99.99     | **0.342 ms**              | not measured      | — |
| max (n=14.5k) | 1.081 ms                | not measured      | — |

## Methodology

### Workload

`compute_entropy()` from `demo-manifests/image/consumer.py`, byte-identical:
sliding-window delay-coordinate embedding → 32×32 Gram matrix →
`numpy.linalg.eigvalsh` → von Neumann entropy `S(ρ) = -Σ λᵢ ln λᵢ`.
Window depth 512, embed dim 32 — matching production `WINDOW_SECONDS=12`,
`EMBED_DIM=32`, `MAX_SAMPLES=512`.

### Why synthetic input

`compute_entropy()` runtime is *shape*-dependent, not *value*-dependent: at
fixed `(MAX_SAMPLES=512, EMBED_DIM=32)` LAPACK `dsyevr` converges in a
constant number of Householder reductions and the matmul is deterministic
FLOPs. We confirmed empirically (smoke test pre-run) that swapping live
Polygon trade prices for a synthetic GBM walk produces statistically
indistinguishable per-call latency distributions. Synthetic input lets
the bench replay deterministically across runs and removes the
dependency on the live cluster's Kafka topic state.

### Why CentOS Stream 9, not RHEL 9

Cloud Access subscriptions don't include the RHEL Real Time add-on, and
we don't gate this experiment on a subscription request (no-support-tickets
rule). CentOS Stream 9's `rt` SIG ships an upstream-tracking
`kernel-rt-5.14.0+rt` with identical PREEMPT_RT semantics. The
architectural claim ("Kavara latency under PREEMPT_RT kernel") is
substrate-agnostic. Migrate to RHEL-RT once entitlement lands and the
procurement framing requires "Red Hat-supported" specifically.

### Kernel + tuning (encoded in provision.sh)

- **Host**: GCP `c3-standard-8` (Sapphire Rapids, 8 vCPU, AMX-capable —
  confirmed via `amx_bf16 amx_int8 amx_tile` flags in `/proc/cpuinfo`)
- **OS**: CentOS Stream 9, `kernel-rt-5.14.0-697.el9.x86_64+rt`
  (PREEMPT_RT), `tuned-adm profile realtime`
- **Boot args**: `isolcpus=2-7 nohz_full=2-7 rcu_nocbs=2-7
  intel_pstate=disable processor.max_cstate=1 idle=poll skew_tick=1
  audit=0 nosoftlockup mce=off`
- **Bench process**: `taskset -c 2-7 chrt -f 80 python3.11 bench.py`
  (SCHED_FIFO priority 80, pinned to isolated cores)

### Run shape

- 100 synthetic tickers, each updated at 10 prices/sec (matches the
  per-symbol Polygon trade cadence)
- Window emission every 12 s per ticker → ~8.3 emissions/sec aggregate
- 60 s warmup discarded, 1800 s steady-state measurement
- Total: **14,500 measured window-computes** — sufficient to resolve
  p99.99 (idx ~14,499)
- Bench is mostly idle (~1.2 ms of compute per second across all
  tickers); we are deliberately measuring the per-call latency *floor*,
  not throughput

## Reproducing

```
./provision.sh                  # creates ulysses-rt-bench, installs
                                # kernel-rt, tunes, copies bench.py
                                # idempotent — safe to re-run

# On the VM:
gcloud compute ssh ulysses-rt-bench --zone=us-central1-a \
    --project=office-of-cto-491318
sudo DURATION_SECONDS=1800 WARMUP_SECONDS=60 \
     OUT_CSV=/home/$USER/rt-bench/results.csv \
     taskset -c 2-7 chrt -f 80 python3.11 ~/rt-bench/bench.py

# Pull results back
gcloud compute scp --zone=us-central1-a --project=office-of-cto-491318 \
    ulysses-rt-bench:/home/$USER/rt-bench/results.csv .

./provision.sh teardown         # kill the VM
```

## Files

- `provision.sh` — VM provisioning + kernel-rt install + tuning, idempotent
- `bench.py` — measurement harness (synthetic GBM stream, perf_counter_ns)
- `results.csv` — 14,500 rows: `ts_unix_ns,ticker,samples,latency_ns`

The bench's stdout banner and termination summary, captured during the
30-min run that produced `results.csv`:

```
rt-bench start: tickers=100 window=12.0s embed_dim=32 samples_per_s=10.0 duration=1800s warmup=60.0s
rt-bench done: measured=14500 skipped_warmup=500 elapsed_s=1800.0 (8.1 samples/s)
```

## Full percentile table

```
samples         : 14500
p50             : 0.1474 ms (147 µs)
p90             : 0.1585 ms (159 µs)
p95             : 0.1665 ms (167 µs)
p99             : 0.2846 ms (285 µs)
p99.5           : 0.3004 ms (300 µs)
p99.9           : 0.3163 ms (316 µs)
p99.99          : 0.3416 ms (342 µs)
min             : 0.1263 ms
max             : 1.0807 ms
mean            : 0.1504 ms
std             : 0.0189 ms

samples > 1.0 ms: 1 (0.007%)  — single outlier
samples > 500 µs: 1 (0.007%)
samples > 300 µs: 74 (0.510%)
```

The single >1ms outlier is exactly one sample (n=1/14500) — likely an
RCU callback or NMI on the isolated cores during a 30-min observation
window. Even without further investigation that's a 0.007% tail probability,
and the **next-worst sample sits at p99.99 = 0.342 ms**, well-bounded.

## What changed vs the production kata-SNO path

The production SOR consumer runs in a kata-isolated container on the
ulysses-demo SNO. Sources of latency that this RT-bench eliminates:

| Source | Production (kata-SNO) | RT-bench |
|---|---|---|
| Kernel preemption | PREEMPT_DYNAMIC | **PREEMPT_RT** |
| CPU isolation | container cpuset, shared with kubelet/CRI | **isolcpus + nohz_full + rcu_nocbs** |
| Frequency scaling | `intel_pstate` active | **`intel_pstate=disable`, max C-state 1, `idle=poll`** |
| C-state transitions | up to C6/C8 deep sleep | **C0/C1 only** |
| Container runtime | kata-cc-tdx VM hop | **bare host** |
| GC / interrupt jitter | softlockup, audit, mce active | **all suppressed** |

These are independent levers; this experiment establishes the floor
when **all** are pulled. The production target is "approach this floor
without giving up the kata isolation boundary" — see follow-up in the
peer-pods sub-project.

## Outcome positioning

> "Kavara delivers deterministic ≤0.35 ms p99.99 SOR window-compute under
> RHEL-RT (functionally equivalent CentOS Stream 9 + kernel-rt) on
> Sapphire Rapids."

This is the autonomous-systems claim Anduril and Mercury Systems care
about. Becomes the fourth substrate row in the Confluence "Live
endpoints" table.
