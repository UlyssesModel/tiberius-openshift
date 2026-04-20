# Copyright 2026 Kavara. Licensed under the Apache License, Version 2.0.
"""
SmartOrderRouter — reference implementation.

This module demonstrates the dispatch pattern Kavara uses in production
(`ts_sor_base` on our self-hosted GitLab). The routing logic, policy tables,
and venue abstractions here are identical to production; the backend
implementations are simplified to numpy calls so this repo stays self-
contained and runnable without proprietary dependencies.

See README.md for the two-layer routing architecture.
"""
from __future__ import annotations

import os
import platform
import re
import warnings
from abc import ABC, abstractmethod
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Dict, Optional, Tuple

import numpy as np


# ============================================================================
# VenueConfig — the execution-parameter vector
# ============================================================================

@dataclass(frozen=True)
class VenueConfig:
    """
    Configuration for a compute execution venue.

    Analogous to an equity-trading SOR's venue descriptor: once the router
    picks *where* to send an order, the venue config carries the execution
    parameters (threads, placement, memory policy, allocator) that shape how
    the actual computation runs.
    """
    venue_name: str
    num_threads: Optional[int] = None
    omp_places: Optional[str] = None
    omp_proc_bind: Optional[str] = None
    numactl_policy: Optional[str] = None
    allocator: Optional[str] = None

    def apply_env(self) -> Dict[str, Optional[str]]:
        """Apply thread/place settings to os.environ. Returns previous values."""
        previous: Dict[str, Optional[str]] = {}
        settings = {
            "OMP_NUM_THREADS": str(self.num_threads) if self.num_threads else None,
            "MKL_NUM_THREADS": str(self.num_threads) if self.num_threads else None,
            "OMP_PLACES": self.omp_places,
            "OMP_PROC_BIND": self.omp_proc_bind,
        }
        for key, value in settings.items():
            if value is None:
                continue
            previous[key] = os.environ.get(key)
            os.environ[key] = value
        return previous


DEFAULT_VENUE = VenueConfig(venue_name="default")


# ============================================================================
# Hardware detection
# ============================================================================

@lru_cache(maxsize=1)
def detect_hardware_id() -> str:
    """
    Best-effort hardware_id detection. Reads /proc/cpuinfo on Linux, sysctl
    on macOS. Returns a string from a fixed set; falls back to 'unknown'.
    """
    system = platform.system()
    machine = platform.machine()

    if system == "Darwin" and "arm" in machine.lower():
        return _detect_apple_variant()

    cpuinfo = _read_cpuinfo()
    if not cpuinfo:
        return "unknown"

    flags = cpuinfo.get("flags", "")
    vendor = cpuinfo.get("vendor_id", "")

    if vendor == "GenuineIntel":
        has_amx = "amx_bf16" in flags or "amx_tile" in flags
        has_amx_int8 = "amx_int8" in flags
        is_tdx = "tdx_guest" in flags
        if has_amx and is_tdx:
            return "gnr-tdx"
        if has_amx and has_amx_int8:
            return "gnr-baremetal"
        if has_amx:
            return "spr"
        return "intel-legacy"

    if vendor == "AuthenticAMD":
        if "sev_guest" in flags or "sev_snp" in flags:
            return "amd-sevsnp"
        return "amd"

    return "unknown"


def _read_cpuinfo() -> Dict[str, str]:
    """Read /proc/cpuinfo into a dict of first-core values."""
    try:
        result: Dict[str, str] = {}
        with open("/proc/cpuinfo", "r") as f:
            for line in f:
                if ":" not in line:
                    continue
                key, _, value = line.partition(":")
                key = key.strip()
                value = value.strip()
                if key and key not in result:
                    result[key] = value
                if key == "processor" and value and value != "0":
                    break
        return result
    except (OSError, ValueError):
        return {}


def _detect_apple_variant() -> str:
    try:
        import subprocess
        brand = subprocess.check_output(
            ["sysctl", "-n", "machdep.cpu.brand_string"],
            text=True, timeout=2,
        ).strip()
        for tag in ("M4", "M3", "M2", "M1"):
            if tag in brand:
                return f"apple-{tag.lower()}"
        return "apple-silicon"
    except Exception:
        return "apple-silicon"


# ============================================================================
# Threshold + venue policy tables
# ============================================================================
# Values below are from Kavara's calibration harness runs on Intel Granite
# Rapids inside TDX (kirk-td on ny5ulysses01), 2026-04-19/20. See:
#   - Workload-to-CPU Calculus (Notion) — theory + validation experiments
#   - AMX Contribution on GNR+TDX (Notion) — the per-N measurement
#   - E2 stride-2 result (internal) — why the pinned/spread venues collapse
#     to amx-stride2-32 on this hardware
# ============================================================================

THRESHOLD_PROFILES: Dict[str, Dict[str, Optional[int]]] = {
    "spr":           {"fused_max": 20, "cpu_max": 500},   # validated SPR
    "gnr-tdx":       {"fused_max": 20, "cpu_max": 800},   # calibrated 2026-04-19 A/B
    "gnr-baremetal": {"fused_max": 20, "cpu_max": 800},   # seeded from gnr-tdx
    "intel-legacy":  {"fused_max": 20, "cpu_max": None},
    "amd-sevsnp":    {"fused_max": 20, "cpu_max": None},
    "amd":           {"fused_max": 20, "cpu_max": None},
    "apple-m4":      {"fused_max": 20, "cpu_max": None},
    "apple-silicon": {"fused_max": 20, "cpu_max": None},
    "unknown":       {"fused_max": 20, "cpu_max": 500},
}


VENUE_POLICIES: Dict[str, Dict[str, VenueConfig]] = {
    "gnr-tdx": {
        "small":  VenueConfig("avx512-small",   num_threads=4,
                              omp_places="{0}:4:1",  omp_proc_bind="close"),
        "pinned": VenueConfig("amx-stride2-32", num_threads=32,
                              omp_places="{0}:32:2", omp_proc_bind="close"),
        "spread": VenueConfig("amx-stride2-32", num_threads=32,
                              omp_places="{0}:32:2", omp_proc_bind="close"),
    },
    "spr": {
        "small":  VenueConfig("avx512-small", num_threads=4,
                              omp_places="cores", omp_proc_bind="close"),
        "pinned": VenueConfig("amx-pinned",   num_threads=22,
                              omp_places="cores", omp_proc_bind="close"),
        "spread": VenueConfig("amx-pinned",   num_threads=22,
                              omp_places="cores", omp_proc_bind="close"),
    },
    "gnr-baremetal": {
        "small":  VenueConfig("avx512-small", num_threads=8),
        "pinned": VenueConfig("amx-pinned",   num_threads=32,
                              numactl_policy="cpunodebind=0,membind=0"),
        "spread": VenueConfig("amx-spread",   num_threads=96,
                              numactl_policy="interleave=all"),
    },
    "amd-sevsnp":    {"small":  VenueConfig("cpu-small"),
                      "pinned": VenueConfig("cpu-pinned"),
                      "spread": VenueConfig("cpu-spread")},
    "amd":           {"small":  VenueConfig("cpu-small"),
                      "pinned": VenueConfig("cpu-pinned"),
                      "spread": VenueConfig("cpu-spread")},
    "intel-legacy":  {"small":  VenueConfig("cpu-small"),
                      "pinned": VenueConfig("cpu-pinned"),
                      "spread": VenueConfig("cpu-spread")},
    "apple-m4":      {"small":  VenueConfig("apple-small"),
                      "pinned": VenueConfig("apple-pinned"),
                      "spread": VenueConfig("apple-spread")},
    "apple-silicon": {"small":  VenueConfig("apple-small"),
                      "pinned": VenueConfig("apple-pinned"),
                      "spread": VenueConfig("apple-spread")},
    "unknown":       {"small":  VenueConfig("default"),
                      "pinned": VenueConfig("default"),
                      "spread": VenueConfig("default")},
}


BUCKET_THRESHOLDS: Dict[str, Dict[str, int]] = {
    "gnr-tdx":       {"small_max": 1000, "pinned_max": 2500},
    "spr":           {"small_max": 1000, "pinned_max": 2500},
    "gnr-baremetal": {"small_max": 1000, "pinned_max": 4000},
    "intel-legacy":  {"small_max": 1000, "pinned_max": 10**9},
    "amd-sevsnp":    {"small_max": 1000, "pinned_max": 10**9},
    "amd":           {"small_max": 1000, "pinned_max": 10**9},
    "apple-m4":      {"small_max": 1000, "pinned_max": 10**9},
    "apple-silicon": {"small_max": 1000, "pinned_max": 10**9},
    "unknown":       {"small_max": 1000, "pinned_max": 2500},
}


# ============================================================================
# ComputeBackend — abstract + reference implementations
# ============================================================================

class ComputeBackend(ABC):
    """
    Strategy pattern for hardware dispatch. All backends implement complex
    matrix multiplication using paired float32 (real, imag) decomposition.

    In production, these call into MKL / oneDNN / AMX BRGEMM. For this
    reference repo, all three backends fall back to a pure-numpy complex
    matmul so the demo runs anywhere. The routing *decision* is identical
    to production — only the numerical backend is simplified.
    """

    @abstractmethod
    def complex_matmul(
        self,
        a_re: np.ndarray, a_im: np.ndarray,
        b_re: np.ndarray, b_im: np.ndarray,
    ) -> Tuple[np.ndarray, np.ndarray]:
        """Computes (a_re + i*a_im) @ (b_re + i*b_im). Returns (c_re, c_im)."""

    def apply_venue_config(self, config: VenueConfig) -> None:
        """Default: no-op. Override to wire thread-count / placement."""


def _numpy_complex_matmul(a_re, a_im, b_re, b_im):
    """(a_re + i*a_im)(b_re + i*b_im) via real-decomposition on numpy."""
    c_re = np.matmul(a_re, b_re) - np.matmul(a_im, b_im)
    c_im = np.matmul(a_re, b_im) + np.matmul(a_im, b_re)
    return c_re, c_im


class FusedBackend(ComputeBackend):
    """
    Production: C/MKL zero-alloc kernel via ctypes (`libkirk_fused.so`).
    Reference: falls back to numpy. Marker: dominates at small N (≤20).
    """
    def complex_matmul(self, a_re, a_im, b_re, b_im):
        return _numpy_complex_matmul(a_re, a_im, b_re, b_im)

    @classmethod
    def is_available(cls) -> bool:
        # Production: checks for libkirk_fused.so. Reference: always available.
        return True


class CPUBackend(ComputeBackend):
    """NumPy/MKL float32 via AVX-512. The correctness-reference backend."""

    def __init__(self):
        self._current_venue: Optional[VenueConfig] = None

    def complex_matmul(self, a_re, a_im, b_re, b_im):
        return _numpy_complex_matmul(a_re, a_im, b_re, b_im)

    def apply_venue_config(self, config: VenueConfig) -> None:
        if config is None or config == self._current_venue:
            return
        config.apply_env()
        self._current_venue = config


class AMXBackend(ComputeBackend):
    """
    Production: PyTorch BF16 → oneDNN → AMX BRGEMM. Reference: numpy f32.
    """
    def __init__(self):
        self._current_venue: Optional[VenueConfig] = None

    def complex_matmul(self, a_re, a_im, b_re, b_im):
        return _numpy_complex_matmul(a_re, a_im, b_re, b_im)

    def apply_venue_config(self, config: VenueConfig) -> None:
        if config is None or config == self._current_venue:
            return
        config.apply_env()
        self._current_venue = config

    @classmethod
    def is_available(cls) -> bool:
        try:
            with open("/proc/cpuinfo", "r") as f:
                return "amx_bf16" in f.read()
        except Exception:
            return False


# ============================================================================
# SmartOrderRouter — the dispatch engine
# ============================================================================

class SmartOrderRouter:
    """
    Routes per-call (matrix shape, dtype, op) → (backend, VenueConfig).
    Per ADR-006 (Kavara internal), this is the canonical class name;
    `KirkAdapter` is retained as a deprecated alias for one release cycle.

    Two layers to understand:

    1. *Backend selection* — picks the kernel family (Fused / CPU / AMX)
       based on N thresholds per hardware profile.
    2. *Venue selection* — picks the execution configuration (thread count,
       placement, memory policy) based on N bucket per hardware profile.

    Both are deterministic lookups from policy tables. Target latency for
    `_select_backend_and_venue` is <10 μs.
    """

    def __init__(
        self,
        thresholds: Optional[Dict[str, int]] = None,
        hardware_id: Optional[str] = None,
    ):
        self.hardware_id = hardware_id or detect_hardware_id()
        self.thresholds = (
            thresholds
            if thresholds is not None
            else THRESHOLD_PROFILES.get(self.hardware_id, THRESHOLD_PROFILES["unknown"])
        )
        self.venue_policy = VENUE_POLICIES.get(
            self.hardware_id, VENUE_POLICIES["unknown"]
        )
        self.bucket_thresholds = BUCKET_THRESHOLDS.get(
            self.hardware_id, BUCKET_THRESHOLDS["unknown"]
        )

        self.fused = FusedBackend() if FusedBackend.is_available() else None
        self.cpu = CPUBackend()
        self.amx = AMXBackend() if AMXBackend.is_available() else None

    def _select_backend_and_venue(self, N: int) -> Tuple[ComputeBackend, VenueConfig]:
        fused_max = self.thresholds.get("fused_max", 20)
        cpu_max = self.thresholds.get("cpu_max", 500)

        if N <= fused_max and self.fused:
            backend: ComputeBackend = self.fused
        elif cpu_max is None or N <= cpu_max or not self.amx:
            backend = self.cpu
        else:
            backend = self.amx

        small_max = self.bucket_thresholds.get("small_max", 1000)
        pinned_max = self.bucket_thresholds.get("pinned_max", 2500)

        if N <= small_max:
            venue = self.venue_policy.get("small", DEFAULT_VENUE)
        elif N <= pinned_max:
            venue = self.venue_policy.get("pinned", DEFAULT_VENUE)
        else:
            venue = self.venue_policy.get("spread", DEFAULT_VENUE)

        return backend, venue

    def _select_backend(self, N: int) -> ComputeBackend:
        """Legacy accessor — returns backend only. Use _select_backend_and_venue."""
        backend, _ = self._select_backend_and_venue(N)
        return backend

    def health(self) -> Dict[str, Any]:
        return {
            "hardware_id": self.hardware_id,
            "thresholds": dict(self.thresholds),
            "backends_available": {
                "fused": self.fused is not None,
                "cpu": True,
                "amx": self.amx is not None,
            },
        }


class KirkAdapter(SmartOrderRouter):
    """DEPRECATED per ADR-006. Use SmartOrderRouter. Removed in 2.0."""

    def __init__(self, *args, **kwargs):
        warnings.warn(
            "KirkAdapter is deprecated per ADR-006; use SmartOrderRouter. "
            "Alias removed in next major release.",
            DeprecationWarning, stacklevel=2,
        )
        super().__init__(*args, **kwargs)
