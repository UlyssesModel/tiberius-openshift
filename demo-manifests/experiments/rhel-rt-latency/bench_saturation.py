#!/usr/bin/env python3
"""Bench compute_entropy() — synthetic Polygon-shaped replay.

Methodology: per JE's earlier observation, compute_entropy()'s runtime is
data-shape-dependent only (closed-form numpy.linalg.eigvalsh on a Hermitian
matrix). Synthetic 32-tick price walks at the production arrival rate
(~33K trades/sec aggregate, ~3.3K/sec/ticker for 10 tickers) drive the
same code path as real Polygon-replayed trades. Each call:

  prices  -> log-returns -> delay-coordinate embedding ->
  Gram matrix -> normalize -> eigvalsh -> -Σ λᵢ ln λᵢ

Mode: as-fast-as-possible back-to-back calls. Captures per-call latency
distribution. Comparison table generated downstream by aggregating across
config runs (default sched / pinned / rt-fifo).

Output CSV: config, call_idx, latency_ns
"""
import argparse
import csv
import time
import sys

import numpy as np


def compute_entropy(returns: np.ndarray, embed_dim: int):
    """Identical to image/consumer.py:compute_entropy."""
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


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--tickers", type=int, default=10)
    p.add_argument("--window-size", type=int, default=32)
    p.add_argument("--embed-dim", type=int, default=8)
    p.add_argument("--duration", type=float, default=30.0)
    p.add_argument("--warmup", type=int, default=2000)
    p.add_argument("--out", required=True)
    p.add_argument("--config", required=True)
    args = p.parse_args()

    rng = np.random.default_rng(42)
    windows = []
    for _ in range(args.tickers):
        prices = (100 + np.cumsum(rng.standard_normal(args.window_size) * 0.5)).astype(
            np.float64
        )
        windows.append(prices)

    # Warm-up: let numpy / kernel dispatch / page allocations settle.
    for _ in range(args.warmup):
        prices = windows[0]
        with np.errstate(invalid="ignore", divide="ignore"):
            returns = np.diff(np.log(prices))
        compute_entropy(returns, args.embed_dim)

    # Bench: back-to-back, alternating ticker, perturbing prices to avoid
    # any branch-prediction cheating from identical-input repeats.
    latencies = []
    end = time.perf_counter() + args.duration
    n = 0
    while time.perf_counter() < end:
        tid = n % args.tickers
        windows[tid][n % args.window_size] += 0.0001 * ((n % 7) - 3)
        prices = windows[tid]
        with np.errstate(invalid="ignore", divide="ignore"):
            returns = np.diff(np.log(prices))
        t0 = time.perf_counter_ns()
        compute_entropy(returns, args.embed_dim)
        t1 = time.perf_counter_ns()
        latencies.append(t1 - t0)
        n += 1

    # Per-call CSV
    with open(args.out, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["config", "call_idx", "latency_ns"])
        for i, lat in enumerate(latencies):
            w.writerow([args.config, i, lat])

    arr = np.array(latencies, dtype=np.int64)
    rate = len(arr) / args.duration
    print(f"config={args.config} calls={len(arr)} duration={args.duration:.1f}s")
    print(f"  effective rate: {rate:,.0f} calls/sec")
    print(f"  p50    : {int(np.percentile(arr, 50)):>10,} ns")
    print(f"  p95    : {int(np.percentile(arr, 95)):>10,} ns")
    print(f"  p99    : {int(np.percentile(arr, 99)):>10,} ns")
    print(f"  p99.9  : {int(np.percentile(arr, 99.9)):>10,} ns")
    print(f"  p99.99 : {int(np.percentile(arr, 99.99)):>10,} ns")
    print(f"  max    : {int(np.max(arr)):>10,} ns")
    print(f"  mean   : {int(np.mean(arr)):>10,} ns")
    print(f"  stddev : {int(np.std(arr)):>10,} ns  <- jitter")


if __name__ == "__main__":
    sys.exit(main())
