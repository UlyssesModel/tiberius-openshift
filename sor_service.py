# Copyright 2026 Kavara. Licensed under the Apache License, Version 2.0.
"""
SmartOrderRouter HTTP service — FastAPI wrapper for OpenShift deployment.

Wraps sor_router.SmartOrderRouter behind HTTP so the outer orchestration
layer (OpenShift Route / Service / Istio) can load-balance across pods,
while the in-process router handles per-call venue dispatch at <10 μs.

Endpoints:
    POST /route     — return the VenueConfig for a given call shape
    POST /compute   — execute complex_matmul with venue applied
    GET  /health    — readiness probe
    GET  /alive     — liveness probe
    GET  /policy    — introspect the active policy tables
    GET  /metrics   — Prometheus text exposition
"""
from __future__ import annotations

import os
import time
from dataclasses import asdict
from typing import Any, Dict, List, Optional

import numpy as np
from fastapi import FastAPI, HTTPException
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from sor_router import SmartOrderRouter, detect_hardware_id


# ============================================================================
# Schemas
# ============================================================================

class RouteRequest(BaseModel):
    n: int = Field(..., description="Matrix dimension N for NxN complex matmul")
    dtype: str = Field(default="bf16", description="Element dtype hint")
    op: str = Field(default="complex_matmul", description="Operation class")


class VenueResponse(BaseModel):
    venue_name: str
    backend: str
    num_threads: Optional[int]
    omp_places: Optional[str]
    omp_proc_bind: Optional[str]
    numactl_policy: Optional[str]
    hardware_id: str


class ComputeRequest(BaseModel):
    a_re: List[List[float]]
    a_im: List[List[float]]
    b_re: List[List[float]]
    b_im: List[List[float]]


class ComputeResponse(BaseModel):
    c_re: List[List[float]]
    c_im: List[List[float]]
    duration_ms: float
    venue: VenueResponse


# ============================================================================
# App
# ============================================================================

app = FastAPI(
    title="Kavara SmartOrderRouter",
    version="1.0.0",
    description=(
        "Reference HTTP service for the Kavara SmartOrderRouter. "
        "Demonstrates the inner layer of the two-layer routing architecture; "
        "the outer layer is OpenShift / Service / Route."
    ),
)

_router: Optional[SmartOrderRouter] = None
_metrics = {
    "route_calls_total": 0,
    "compute_calls_total": 0,
    "compute_errors_total": 0,
}


@app.on_event("startup")
async def startup() -> None:
    global _router
    hw_id_override = os.environ.get("KAVARA_HARDWARE_ID", "").strip() or None
    _router = SmartOrderRouter(hardware_id=hw_id_override)
    print(
        f"[sor] ready — hardware_id={_router.hardware_id} "
        f"thresholds={dict(_router.thresholds)}"
    )


@app.post("/route", response_model=VenueResponse)
async def route_call(req: RouteRequest) -> VenueResponse:
    if _router is None:
        raise HTTPException(503, "router not initialized")
    _metrics["route_calls_total"] += 1
    backend, venue = _router._select_backend_and_venue(req.n)
    return VenueResponse(
        venue_name=venue.venue_name,
        backend=type(backend).__name__,
        num_threads=venue.num_threads,
        omp_places=venue.omp_places,
        omp_proc_bind=venue.omp_proc_bind,
        numactl_policy=venue.numactl_policy,
        hardware_id=_router.hardware_id,
    )


@app.post("/compute", response_model=ComputeResponse)
async def compute(req: ComputeRequest) -> ComputeResponse:
    if _router is None:
        raise HTTPException(503, "router not initialized")
    _metrics["compute_calls_total"] += 1
    try:
        a_re = np.asarray(req.a_re, dtype=np.float32)
        a_im = np.asarray(req.a_im, dtype=np.float32)
        b_re = np.asarray(req.b_re, dtype=np.float32)
        b_im = np.asarray(req.b_im, dtype=np.float32)
        if a_re.shape[0] != a_re.shape[1]:
            raise HTTPException(400, f"expected square matrix, got {a_re.shape}")
        n = a_re.shape[0]

        backend, venue = _router._select_backend_and_venue(n)
        backend.apply_venue_config(venue)

        t0 = time.perf_counter_ns()
        c_re, c_im = backend.complex_matmul(a_re, a_im, b_re, b_im)
        duration_ms = (time.perf_counter_ns() - t0) / 1e6

        return ComputeResponse(
            c_re=c_re.tolist(),
            c_im=c_im.tolist(),
            duration_ms=round(duration_ms, 3),
            venue=VenueResponse(
                venue_name=venue.venue_name,
                backend=type(backend).__name__,
                num_threads=venue.num_threads,
                omp_places=venue.omp_places,
                omp_proc_bind=venue.omp_proc_bind,
                numactl_policy=venue.numactl_policy,
                hardware_id=_router.hardware_id,
            ),
        )
    except HTTPException:
        raise
    except Exception as exc:
        _metrics["compute_errors_total"] += 1
        raise HTTPException(500, f"compute failed: {exc!r}")


@app.get("/health")
async def health() -> Dict[str, Any]:
    if _router is None:
        raise HTTPException(503, "not ready — router uninitialized")
    return {
        "status": "ready",
        "hardware_id": _router.hardware_id,
        "backends_available": {
            "fused": _router.fused is not None,
            "cpu": _router.cpu is not None,
            "amx": _router.amx is not None,
        },
    }


@app.get("/alive")
async def alive() -> Dict[str, str]:
    return {"status": "alive"}


@app.get("/policy")
async def policy_info() -> Dict[str, Any]:
    if _router is None:
        raise HTTPException(503, "not ready")
    return {
        "hardware_id": _router.hardware_id,
        "thresholds": dict(_router.thresholds),
        "bucket_thresholds": dict(_router.bucket_thresholds),
        "venue_policy": {
            bucket: asdict(venue)
            for bucket, venue in _router.venue_policy.items()
        },
    }


@app.get("/metrics", response_class=PlainTextResponse)
async def metrics() -> str:
    hw = _router.hardware_id if _router is not None else "uninitialized"
    ready = 1 if _router is not None else 0
    lines = [
        "# HELP sor_ready 1 if the SmartOrderRouter is initialized and ready",
        "# TYPE sor_ready gauge",
        f'sor_ready{{hardware_id="{hw}"}} {ready}',
        "# HELP sor_route_calls_total Number of /route calls received",
        "# TYPE sor_route_calls_total counter",
        f'sor_route_calls_total{{hardware_id="{hw}"}} {_metrics["route_calls_total"]}',
        "# HELP sor_compute_calls_total Number of /compute calls received",
        "# TYPE sor_compute_calls_total counter",
        f'sor_compute_calls_total{{hardware_id="{hw}"}} {_metrics["compute_calls_total"]}',
        "# HELP sor_compute_errors_total Number of /compute errors",
        "# TYPE sor_compute_errors_total counter",
        f'sor_compute_errors_total{{hardware_id="{hw}"}} {_metrics["compute_errors_total"]}',
    ]
    return "\n".join(lines) + "\n"
