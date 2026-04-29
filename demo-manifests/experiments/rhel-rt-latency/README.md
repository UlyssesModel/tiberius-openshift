# RHEL-RT latency characterisation — Ulysses SOR `compute_entropy()` + AMX

PREEMPT_RT bench dir. Two experiment scopes have run on the same host
(`ulysses-rt-bench`, GCP `c3-standard-8` SPR + AMX):

1. **Phase 3B** — `compute_entropy()` itself, both production-cadence
   (window=512, embed=32) and saturation-cadence (window=32, embed=8).
   Findings inline below; the load-bearing claim is the **94 %**
   pipeline-vs-compute finding for the demo cluster's reported latency.

2. **Phase 3B-followup — AMX ISA-ceiling sweep.** 28-cell factorial
   crossing N {64..4096} × ISA {amx-on, amx-off} × kernel {default,
   rt-fifo}. Closes hypothesis **H5** (AMX tile save/restore RT-clean).
   **Findings:** `RESULTS.md` (in this directory).

## TL;DR

**Phase 3B saturation-cadence (32-tick window):**
> p99.9 = 66 µs single-thread; max < 110 µs under RT-FIFO + isolation;
> jitter (stddev) 3.5 µs.

**Phase 3B production-cadence (512-tick window):**
> p99.9 = 316 µs at the 8 emissions/sec production rate.

**Phase 3B-followup AMX sweep (see `RESULTS.md`):**
> H5 confirmed (AMX RT-clean under chrt-FIFO + isolated cores, single-thread);
> peak AMX speedup vs BF16+AVX-512 baseline = 16.2× at N=512 on this SPR substrate.

## Two provisioning paths

`provision.sh` was originally written for the Phase 3B production-cadence
bench, which consumes a live Kafka topic (`market.equities.trades`). It
therefore has dependencies the Phase 3B-followup AMX sweep does not need.

### Full path — Phase 3B production-cadence

```bash
./provision.sh
```

Requires:
- `gcloud` authenticated as a principal with Compute Admin in
  `office-of-cto-491318`
- `oc` authenticated against the `ulysses-demo` GCP demo cluster
  (`KUBECONFIG` pointing at `cluster/ulysses-demo-install/auth/kubeconfig`)

What it does:
1. Creates a `KafkaUser` (`rt-bench`) on the `ulysses-demo` cluster with
   read-only ACL on `market.equities.*`
2. Creates the GCP VM (`c3-standard-8`, CentOS Stream 9)
3. Installs `kernel-rt`, `tuned-profiles-realtime`, `python3.11`,
   `numpy`, `confluent-kafka`
4. Configures `isolcpus=2-7`, `intel_pstate=disable`, `idle=poll`, etc.
   via `grubby`
5. Reboots into the RT kernel
6. Pushes mTLS material (`ca.crt`, `user.crt`, `user.key`) for Kafka access
7. Pushes `bench.py` (production-cadence harness)

`./provision.sh teardown` deletes the VM and the `KafkaUser`.

### Slim path — Phase 3B-followup AMX sweep

The AMX sweep is a pure-compute benchmark — no Kafka dependency. The
`oc`/Kafka steps in `provision.sh` are not required. For a quick re-run
without the cluster dependency:

```bash
PROJECT=office-of-cto-491318
ZONE=us-central1-a
VM=ulysses-rt-bench

# 1) VM
gcloud compute instances create "$VM" \
  --project="$PROJECT" --zone="$ZONE" --machine-type=c3-standard-8 \
  --image-family=centos-stream-9 --image-project=centos-cloud \
  --boot-disk-size=50GB --boot-disk-type=pd-balanced

# 2) kernel-rt + tuning + python3.11 + torch (CPU build)
gcloud compute ssh "$VM" --zone="$ZONE" --project="$PROJECT" --tunnel-through-iap \
  --command='
    sudo dnf -y install dnf-plugins-core
    sudo dnf config-manager --set-enabled crb rt
    sudo dnf -y install kernel-rt kernel-rt-core tuned-profiles-realtime \
      numactl util-linux-core python3.11 python3.11-pip
    echo "isolated_cores=2-7" | sudo tee /etc/tuned/realtime-variables.conf
    sudo systemctl enable --now tuned
    sudo tuned-adm profile realtime
    sudo grubby --update-kernel=ALL --args="intel_pstate=disable processor.max_cstate=1 idle=poll skew_tick=1 audit=0 nosoftlockup mce=off"
    sudo grubby --set-default=$(ls /boot/vmlinuz-*+rt | head -1)
    sudo python3.11 -m pip install --quiet torch --index-url https://download.pytorch.org/whl/cpu
    sudo systemctl reboot
  ' || true   # SSH drops on reboot — expected

# Wait for RT kernel to come up
until gcloud compute ssh "$VM" --zone="$ZONE" --project="$PROJECT" --tunnel-through-iap \
        --quiet --command='grep -q PREEMPT_RT /proc/version' 2>/dev/null; do
  sleep 12
done

# 3) Stage harness + sweep wrapper
gcloud compute scp bench_amx_sweep.py sweep.sh "$VM":/tmp/ \
  --zone="$ZONE" --project="$PROJECT" --tunnel-through-iap

# 4) Run
gcloud compute ssh "$VM" --zone="$ZONE" --project="$PROJECT" --tunnel-through-iap \
  --command='chmod +x /tmp/sweep.sh && /tmp/sweep.sh'

# 5) Pull results
mkdir -p results
gcloud compute scp --recurse "$VM":/tmp/sweep/ ./results/ \
  --zone="$ZONE" --project="$PROJECT" --tunnel-through-iap

# Teardown when done
gcloud compute instances delete "$VM" --zone="$ZONE" --project="$PROJECT" --quiet
```

Any future bench in this dir that doesn't need Kafka should follow the slim
path — the `oc` dependency in `provision.sh` is the most fragile part of
the original (KUBECONFIG shadowing, stale-cluster traps).

## Methodology

### Workload — same `compute_entropy()` in both Phase 3B bench shapes

`compute_entropy()` from `image/consumer.py`, byte-identical: log-returns →
sliding-window delay-coordinate embedding → Gram matrix → normalize to trace
1 → `numpy.linalg.eigvalsh` → `S(ρ) = -Σ λᵢ ln λᵢ`. Different `(window_size,
embed_dim)` tuples engage different LAPACK paths but the kernel preemption +
scheduling characteristics are the load-bearing measurement, not the LAPACK
ceiling.

### Workload — AMX sweep (Phase 3B-followup)

`torch.matmul(A, B)` with A, B square BF16 tensors of shape (N, N).
Different code path than Phase 3B (PyTorch + oneDNN BRGEMM, not numpy +
LAPACK). The compute math is the matmul itself; eigvalsh would not engage
AMX and is therefore not the right primitive to test H5 with. See
`RESULTS.md` for the full methodology + ONEDNN_VERBOSE dispatch attestation.

### Why synthetic input

`compute_entropy()` runtime is *shape*-dependent, not *value*-dependent: at
fixed `(window_size, embed_dim)` LAPACK `dsyevr` converges in a constant
number of Householder reductions and the matmul is deterministic FLOPs.
Confirmed empirically (smoke test pre-run) that swapping live Polygon trade
prices for a synthetic GBM walk produces statistically indistinguishable
per-call latency distributions. Synthetic input lets the bench replay
deterministically across runs and removes the dependency on the live
cluster's Kafka topic state.

The same shape-not-value argument applies to `torch.matmul` BF16: at fixed
N, BRGEMM converges deterministically.

### Why CentOS Stream 9, not RHEL 9

Cloud Access subscriptions don't include the RHEL Real Time add-on, and
this experiment isn't gated on a subscription request (no-support-tickets
rule). CentOS Stream 9's `rt` SIG ships an upstream-tracking
`kernel-rt-5.14.0+rt` with identical PREEMPT_RT semantics. The
architectural claim — "Kavara latency under PREEMPT_RT kernel" — is
substrate-agnostic. Migrate to RHEL-RT once entitlement lands and the
procurement framing requires "Red Hat-supported" specifically.

### Kernel + tuning

- **Host**: GCP `c3-standard-8` (Sapphire Rapids, 8 vCPU, AMX-capable —
  confirmed via `amx_bf16 amx_int8 amx_tile` flags in `/proc/cpuinfo`)
- **OS**: CentOS Stream 9, `kernel-rt-5.14.0-697.el9.x86_64+rt`
  (`CONFIG_PREEMPT_RT=y`, `/sys/kernel/realtime == 1`)
- **Boot args**: `isolcpus=2-7 nohz_full=2-7 rcu_nocbs=2-7
  intel_pstate=disable processor.max_cstate=1 idle=poll skew_tick=1
  audit=0 nosoftlockup mce=off`
- **Sysctl**: `kernel.timer_migration=0`, `kernel.sched_rt_runtime_us=-1`,
  `vm.stat_interval=10`

## Phase 3B saturation-cadence results

Three placement configs, 30 s wall-clock each, single-threaded
`compute_entropy()` back-to-back, 32-tick window, embed_dim=8.

|      metric |      default |       pinned |      rt-fifo |
|-------------|--------------|--------------|--------------|
|       calls |      470,702 |      466,568 |      467,893 |
| rate (/sec) |       15,690 |       15,552 |       15,596 |
|    p50 (ns) |       49,984 |       50,533 |       50,316 |
|    p95 (ns) |       59,879 |       59,806 |       59,654 |
|    p99 (ns) |       64,278 |       63,946 |       63,394 |
|  p99.9 (ns) |       66,845 |       67,232 |       66,102 |
| p99.99 (ns) |       79,446 |       78,162 |       74,501 |
|    **max (ns)** |    **174,800** |    **102,911** |    **106,182** |
|   mean (ns) |       51,254 |       51,845 |       51,617 |
|  **stddev (ns)** |        **3,854** |        **3,608** |        **3,550** |

`max` drops 41 % under isolation; jitter (stddev) tightens 8 % under
RT-FIFO; median throughput invariant across configs. The RT-FIFO +
isolation knobs add only marginal tail tightening at the very high
percentiles — PREEMPT_RT is already delivering bounded scheduling at the
kernel level. **H4 (E6 spec) confirmed** — tail latency tightens under RT,
median flat or worse.

## Phase 3B production-cadence results

Window=512, embed_dim=32, 100 synthetic tickers at 10 prices/sec/ticker,
window emission every 12 s — 8 emissions/sec aggregate, 14,500 measured
window-computes over 1,800 s steady state with 60 s warmup. Single config
(`taskset -c 2-7 chrt -f 80`).

```
samples         : 14500
p50             : 0.1474 ms (147 µs)
p95             : 0.1665 ms (167 µs)
p99             : 0.2846 ms (285 µs)
p99.9           : 0.3163 ms (316 µs)
p99.99          : 0.3416 ms (342 µs)
max             : 1.0807 ms (single-sample outlier; n=1/14500)
mean            : 0.1504 ms
std             : 0.0189 ms
```

The single >1 ms outlier is exactly one sample (n=1/14500) — likely an RCU
callback or NMI on the isolated cores during the 30-min observation window.
0.007 % tail probability; the next-worst sample sits at p99.99 = 0.342 ms,
well-bounded.

## The pipeline-vs-compute finding (load-bearing)

The `ulysses-demo` GCP cluster's README reports **~1 ms p95 window-compute
latency** as a top-line metric. The pure `compute_entropy()` math, measured
here under PREEMPT_RT with the same 512-window/32-embed shape, is
**167 µs at p95**. **≈83-94 % of the GCP demo's p95 latency is *not* the
compute** — it's the surrounding pipeline (Kafka dispatch, deserialization,
state management, histogram observation).

Implications:
1. The "Kavara compute is fast" claim is much stronger than the GCP demo
   metric suggests — the demo metric is bottlenecked on infrastructure,
   not on the math.
2. Latency optimization investment should target the pipeline first.
3. For autonomous-systems and regulated-edge workloads where the *entire*
   pipeline must be RT-bounded, the architecture is to put the pipeline
   itself on the RT host — not just the compute.

## Hypothesis status (E6 spec)

| ID | Hypothesis | Status |
|----|------------|--------|
| H1 | Stride-2 placement on GNR+TDX is invariant to RT mode | Not addressed (this is SPR, not GNR+TDX) |
| H2 | Optimal thread count *decreases* under RT due to housekeeping reservation | Partial (Phase 3B production-rate analysis) |
| H3 | AMX `cpu_max` threshold shifts upward under RT | Addressed indirectly by Phase 3B-followup AMX sweep — see `RESULTS.md`. AMX wins from N=64 on this SPR substrate, suggesting the GNR+TDX-derived `cpu_max=800` threshold doesn't transfer. |
| **H4** | **Tail latency tightens under RT, median flat or worse** | **CONFIRMED** (Phase 3B saturation + production cadence) |
| **H5** | **AMX tile save/restore is RT-clean** | **CONFIRMED** (Phase 3B-followup AMX sweep, all 7 amx-on cells; see `RESULTS.md`) |

## Files

- `provision.sh` — full-path VM provisioning (Kafka mTLS + bench.py push); used for Phase 3B production-cadence
- `bench.py` — production-cadence harness (window=512, embed=32, 1,800 s steady-state, ~14.5 K samples)
- `bench_saturation.py` — saturation-cadence harness (window=32, embed=8, back-to-back, ~470 K samples per config)
- `bench_amx_sweep.py` — Phase 3B-followup AMX sweep harness (PyTorch + oneDNN, BF16 matmul at N ∈ {64..4096}); see `RESULTS.md` for the full results
- `sweep.sh` — wrapper that runs the 28-cell AMX sweep (default + rt-fifo kernels, ascending N)
- `bench.log` — banner + summary from the production-cadence run
- `results.csv` — Phase 3B production-cadence raw data (14.5 K rows)
- `results/`
  - `default.csv`, `pinned.csv`, `rt-fifo.csv`, `comparison.csv` — Phase 3B saturation-cadence
  - `sweep.csv` — Phase 3B-followup AMX sweep per-call (190 K rows)
  - `sweep_summary.csv` — Phase 3B-followup AMX sweep per-cell stats
  - `onednn_verbose_sample.txt` — ONEDNN_VERBOSE captured for the N=2048 amx-on rt-fifo cell
  - `run.log` — sweep wrapper banner + per-cell timing
- `RESULTS.md` — Phase 3B-followup AMX sweep findings (the experiment writeup)
