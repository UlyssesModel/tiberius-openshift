#!/usr/bin/env python3.11
"""AMX ISA-ceiling sweep harness — Phase 3B-followup, H5 closure.

Methodology (per JE's 2026-04-29 framing):

  Bench `torch.matmul(A, B)` with A, B square BF16 tensors of size NxN. The
  same BF16 input is dispatched by oneDNN to two different code paths
  controlled by `ONEDNN_MAX_CPU_ISA`:

    - unset           -> brg_matmul:avx10_1_512_amx       (AMX BF16 kernel)
    - AVX512_CORE     -> gemm:jit:bf16                    (AVX-512 BF16, no AMX)

H5 (verbatim from Phase 3B README): "AMX tile save/restore is RT-clean
(no jitter from XSAVE/XRSTOR on context switch)."

Per-call timing: time.perf_counter_ns() bracketing exactly the matmul call.
Pre-allocated tensors; warm-up; back-to-back; no Python-level allocator
churn between samples.

Output schema:
  per-call CSV (--out, append-mode):
    cell_id, N, isa, kernel, sample_idx, latency_us
  per-cell summary CSV (--summary, append-mode):
    cell_id, N, isa, kernel, n_samples, p50_us, p95_us, p99_us, p999_us, max_us, mean_us
"""
from __future__ import annotations

import argparse
import csv
import os
import sys
import time

import torch


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--N", type=int, required=True)
    p.add_argument("--isa", choices=["amx-on", "amx-off"], required=True)
    p.add_argument("--kernel", choices=["default", "rt-fifo"], required=True)
    p.add_argument("--cell-id", required=True)
    p.add_argument("--samples", type=int, default=10000)
    p.add_argument("--max-seconds", type=int, default=120)
    p.add_argument("--warmup", type=int, default=50)
    p.add_argument("--out", required=True)
    p.add_argument("--summary", required=True)
    args = p.parse_args()

    torch.manual_seed(0xCAFEBABE)
    A = torch.randn(args.N, args.N).bfloat16().contiguous()
    B = torch.randn(args.N, args.N).bfloat16().contiguous()

    for _ in range(args.warmup):
        _ = A @ B

    latencies = []
    deadline = time.perf_counter() + args.max_seconds
    while len(latencies) < args.samples and time.perf_counter() < deadline:
        t0 = time.perf_counter_ns()
        _ = A @ B
        t1 = time.perf_counter_ns()
        latencies.append(t1 - t0)

    write_header_out = not os.path.exists(args.out)
    with open(args.out, "a", newline="") as f:
        w = csv.writer(f)
        if write_header_out:
            w.writerow(["cell_id", "N", "isa", "kernel", "sample_idx", "latency_us"])
        for i, lat_ns in enumerate(latencies):
            w.writerow([args.cell_id, args.N, args.isa, args.kernel, i, f"{lat_ns / 1000.0:.3f}"])

    n = len(latencies)
    arr_sorted = sorted(latencies)
    def pct_us(p_):
        return arr_sorted[min(int(p_ / 100.0 * n), n - 1)] / 1000.0 if n else 0.0

    p50 = pct_us(50)
    p95 = pct_us(95)
    p99 = pct_us(99)
    p999 = pct_us(99.9)
    mx = max(latencies) / 1000.0 if n else 0.0
    mean = sum(latencies) / n / 1000.0 if n else 0.0

    write_header_sum = not os.path.exists(args.summary)
    with open(args.summary, "a", newline="") as f:
        w = csv.writer(f)
        if write_header_sum:
            w.writerow([
                "cell_id", "N", "isa", "kernel", "n_samples",
                "p50_us", "p95_us", "p99_us", "p999_us", "max_us", "mean_us",
            ])
        w.writerow([
            args.cell_id, args.N, args.isa, args.kernel, n,
            f"{p50:.3f}", f"{p95:.3f}", f"{p99:.3f}",
            f"{p999:.3f}", f"{mx:.3f}", f"{mean:.3f}",
        ])

    capped = "CAPPED" if n < args.samples else "FULL"
    print(
        f"[{capped}] {args.cell_id}: n={n} "
        f"p50={p50:.1f}us p95={p95:.1f}us p99={p99:.1f}us "
        f"p99.9={p999:.1f}us max={mx:.1f}us mean={mean:.1f}us"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
