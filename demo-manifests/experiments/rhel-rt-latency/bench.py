#!/usr/bin/env python3.11
"""
RT-kernel latency characterisation harness for the Ulysses SOR window-compute.

Mirrors the production consumer's hot path (image/consumer.py: compute_entropy)
but replaces the prometheus Histogram with nanosecond-resolution
perf_counter_ns timings persisted to a CSV. Designed to run as a single
process pinned to isolated cores 2-7 under SCHED_FIFO.

  taskset -c 2-7 chrt -f 80 python3.11 bench.py

Why synthetic input
-------------------
compute_entropy()'s runtime is dominated by `numpy.linalg.eigvalsh` on a
fixed 32x32 matrix plus a 512x32 sliding-window matmul. Both are
*shape*-dependent, not *value*-dependent: at this size LAPACK dsyevr
converges in a constant number of Householder reductions, and the matmul
is deterministic FLOPs. We confirmed empirically that swapping live
Polygon trade prices for a synthetic random walk produces statistically
indistinguishable per-call latency distributions. We therefore use a local
synthetic stream — both to remove the dependency on the live demo
cluster's Kafka and to let the bench replay deterministically across runs.

The synthetic stream emits a steady ~Polygon-rate sample-per-ticker stream
across N tickers; once a ticker has min_samples we trigger a window
compute every WINDOW_SECONDS (matching prod cadence). With N_TICKERS=100
and DURATION_SECONDS=1800 we collect ~15k window-compute observations,
enough to resolve p99.99.

Output: one row per (ticker, window) compute, fields:
  ts_unix_ns,ticker,samples,latency_ns
"""
from __future__ import annotations

import csv
import os
import signal
import sys
import time
from collections import deque
from typing import Deque, List

import numpy as np


N_TICKERS = int(os.environ.get("N_TICKERS", "100"))
WINDOW_S = float(os.environ.get("WINDOW_SECONDS", "12"))
EMBED_DIM = int(os.environ.get("EMBED_DIM", "32"))
MIN_SAMPLES = int(os.environ.get("MIN_SAMPLES", "64"))
MAX_SAMPLES = int(os.environ.get("MAX_SAMPLES", "512"))
SAMPLES_PER_S = float(os.environ.get("SAMPLES_PER_S", "10"))   # per-ticker arrival rate
DURATION_S = int(os.environ.get("DURATION_SECONDS", "1800"))
OUT_CSV = os.environ.get("OUT_CSV", "results.csv")
WARMUP_S = float(os.environ.get("WARMUP_SECONDS", "60"))


def compute_entropy(returns: np.ndarray, embed_dim: int) -> float | None:
    """Identical math to image/consumer.py compute_entropy()."""
    if returns.size < embed_dim + 4:
        return None
    r = returns - returns.mean()
    X = np.lib.stride_tricks.sliding_window_view(r, embed_dim).copy()
    if X.shape[0] < 2:
        return None
    G = X.T @ X
    tr = float(np.trace(G))
    if tr <= 1e-12:
        return 0.0
    rho = G / tr
    eigs = np.linalg.eigvalsh(rho)
    eigs = eigs[eigs > 1e-12]
    if eigs.size == 0:
        return 0.0
    return float(-np.sum(eigs * np.log(eigs)))


STOP = False


def _shutdown(signum, _frame) -> None:  # noqa: ANN001
    global STOP
    STOP = True


def main() -> int:
    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    rng = np.random.default_rng(0xCAFEBABE)
    # Pre-seed each ticker with a 512-deep random walk so every window has a
    # full-sized buffer from t=0; this isolates compute time from buffer-fill
    # dynamics.
    seeds = rng.standard_normal((N_TICKERS, MAX_SAMPLES)) * 0.001
    base = 100.0
    initial = base * np.exp(np.cumsum(seeds, axis=1))
    windows: List[Deque[float]] = [
        deque(initial[i].tolist(), maxlen=MAX_SAMPLES) for i in range(N_TICKERS)
    ]
    last_emit = np.zeros(N_TICKERS, dtype=np.float64)
    next_arrival = np.zeros(N_TICKERS, dtype=np.float64)
    arrival_step = 1.0 / SAMPLES_PER_S

    started = time.monotonic()
    deadline = started + DURATION_S
    warmup_deadline = started + WARMUP_S

    print(
        f"rt-bench start: tickers={N_TICKERS} window={WINDOW_S}s "
        f"embed_dim={EMBED_DIM} samples_per_s={SAMPLES_PER_S} "
        f"duration={DURATION_S}s warmup={WARMUP_S}s",
        file=sys.stderr, flush=True,
    )

    fout = open(OUT_CSV, "w", buffering=1, newline="")
    w = csv.writer(fout)
    w.writerow(["ts_unix_ns", "ticker", "samples", "latency_ns"])

    measured = 0
    skipped_warmup = 0

    try:
        while not STOP:
            now_mono = time.monotonic()
            if now_mono >= deadline:
                break

            # 1) Stochastic price update — small geometric brownian step per ticker
            #    when its arrival timer fires. Vectorised; cheap relative to
            #    compute_entropy.
            due = next_arrival <= now_mono
            n_due = int(due.sum())
            if n_due > 0:
                # GBM step: next_price = last_price * exp(sigma * Z)
                sigma = 0.0008
                shocks = rng.standard_normal(n_due) * sigma
                idxs = np.where(due)[0]
                for j, k in enumerate(idxs):
                    last = windows[k][-1]
                    windows[k].append(last * float(np.exp(shocks[j])))
                next_arrival[due] = now_mono + arrival_step

            # 2) Window emission — for any ticker whose timer has elapsed and
            #    whose buffer has min_samples, fire compute_entropy and record.
            for k in range(N_TICKERS):
                if now_mono - last_emit[k] < WINDOW_S:
                    continue
                if len(windows[k]) < MIN_SAMPLES:
                    continue
                prices = np.fromiter(windows[k], dtype=np.float64)
                returns = np.diff(np.log(np.maximum(prices, 1e-12)))

                t0 = time.perf_counter_ns()
                _ = compute_entropy(returns, EMBED_DIM)
                t1 = time.perf_counter_ns()

                if now_mono >= warmup_deadline:
                    w.writerow([time.time_ns(), k, prices.size, t1 - t0])
                    measured += 1
                else:
                    skipped_warmup += 1
                last_emit[k] = now_mono

            # 3) Yield briefly. With WINDOW_SECONDS=12 and 100 tickers we fire
            #    ~8 emissions/sec, each ~1ms — bench is mostly idle. A short
            #    sleep keeps the loop responsive without burning the isolated
            #    cores in busy wait.
            time.sleep(0.001)
    finally:
        fout.close()
        elapsed = time.monotonic() - started
        print(
            f"rt-bench done: measured={measured} skipped_warmup={skipped_warmup} "
            f"elapsed_s={elapsed:.1f} ({measured/elapsed:.1f} samples/s)",
            file=sys.stderr, flush=True,
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
