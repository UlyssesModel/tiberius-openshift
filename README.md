# Kavara SmartOrderRouter — OpenShift reference deployment

**A Smart Order Router for compute.** Takes a compute call (matrix shape, dtype, op) and routes it to the best compute venue (kernel family × thread count × placement × memory policy) for the underlying hardware. Same mental model as an equity-trading SOR: one system decides where work goes, another executes.

This repository is a **reference deployment** of the architecture — clean enough to clone, build, and deploy onto an OpenShift cluster without requiring any proprietary Kavara dependencies. Production code for the full model lives in Kavara's self-hosted GitLab and carries additional numerical optimizations (C/MKL fused kernels, AMX BRGEMM, full torch/oneDNN dispatch). The dispatch *logic* here is identical to production.

## The two-layer routing picture

```
Client request
     ↓
[OUTER LAYER — OpenShift]
  Route / Service / Ingress / Istio service mesh
  • Load-balances across pods
  • Routes requests to the right TD / hardware_id / tenant
  • Horizontal scaling via HPA
  • Time scale: 100 μs – 10 ms
     ↓
[INNER LAYER — SmartOrderRouter, inside each pod]
  SmartOrderRouter._select_backend_and_venue(shape, dtype, op, hardware_id)
  • Per-call dispatch to the best compute venue
  • Calibrated policy tables per hardware profile
  • Target latency: <10 μs
     ↓
Compute (FusedBackend / CPUBackend / AMXBackend / future: GPU, NPU, q)
```

The two layers stack. OpenShift does not know what AMX is; the SOR does not know what a Kubernetes pod is. Same role: routing. Different time scale, different vocabulary, cleanly composable.

## What's in this repository

| File | Purpose |
| --- | --- |
| [`sor_router.py`](./sor_router.py) | Reference `SmartOrderRouter` + `VenueConfig` + backends. Hardware detection. Calibrated policy tables. |
| [`sor_service.py`](./sor_service.py) | FastAPI HTTP wrapper. Endpoints: `/route`, `/compute`, `/health`, `/alive`, `/policy`, `/metrics`. |
| [`Dockerfile`](./Dockerfile) | Multi-stage build on `ubi9/python-312`, non-root UID 1001 (OpenShift SCC-compliant). |
| [`requirements.txt`](./requirements.txt) | FastAPI + uvicorn + pydantic + numpy. No proprietary dependencies. |
| [`deployment.yaml`](./deployment.yaml) | Nine-resource OpenShift manifest: Namespace, ConfigMap, Deployment, Service, Route, HPA, NetworkPolicy, ServiceMonitor, RuntimeClass (commented for Kata-CC + TDX). |
| [`LICENSE`](./LICENSE) | Apache License 2.0. |

## Quick start — local

```bash
# Build
docker build -t kavara/sor-service:reference .

# Run (detected as 'unknown' hardware_id on a Mac, 'spr'/'gnr-tdx' on Intel Linux)
docker run -p 8080:8080 kavara/sor-service:reference

# Exercise
curl -s http://localhost:8080/health | jq
curl -s http://localhost:8080/policy | jq
curl -s -X POST http://localhost:8080/route \
    -H 'Content-Type: application/json' \
    -d '{"n": 2000}' | jq
```

Expected `/route` response on hardware_id `gnr-tdx`:

```json
{
  "venue_name": "amx-stride2-32",
  "backend": "AMXBackend",
  "num_threads": 32,
  "omp_places": "{0}:32:2",
  "omp_proc_bind": "close",
  "numactl_policy": null,
  "hardware_id": "gnr-tdx"
}
```

## Quick start — OpenShift

```bash
# Push the image to the internal registry
oc registry login
IMAGE=$(oc registry info)/kavara-sor/sor-service:v1.0.0
docker tag kavara/sor-service:reference "$IMAGE"
docker push "$IMAGE"

# Apply the manifest
oc apply -f deployment.yaml

# Watch it come up
oc get pods -n kavara-sor --watch

# Hit the Route
ROUTE=$(oc get route sor -n kavara-sor -o jsonpath='{.spec.host}')
curl -s https://$ROUTE/health | jq
```

Expected: 3 replicas scheduled onto AMX-capable nodes (via NFD labels), spread across topology zones, Prometheus-scraped automatically via ServiceMonitor, externally addressable via edge-TLS Route.

## OpenShift-native choices in the manifest

Ten decisions worth understanding on a first read of `deployment.yaml`:

1. **Node Feature Discovery integration.** `nodeSelector: feature.node.kubernetes.io/cpu-cpuid.AMX_BF16: "true"` — pods only schedule onto AMX-capable nodes. Requires the NFD operator installed.
2. **NUMA-aware replica spread.** `topologySpreadConstraints` with `topology.kubernetes.io/zone` — K8s-native way to implement Kavara's "Path 3" production architecture (multiple smaller TDs, one per NUMA sub-node). Extend with `feature.node.kubernetes.io/cpu-topology-numa-node` if your NFD surfaces it.
3. **Thread placement via env vars.** `OMP_NUM_THREADS=32`, `OMP_PLACES={0}:32:2`, `OMP_PROC_BIND=close` — these values are the output of Kavara's calibration harness (E2 experiment, 2026-04-20). Stride-2 placement of 32 threads across 64 vCPUs is the calibrated optimum for GNR+TDX; beats packed-64 by 7% at N=4000 and packed-lower-32 by 19%.
4. **Policy version as Prometheus label.** The ConfigMap holds `POLICY_VERSION` which becomes a metric label — enables cluster-wide drift detection across pods with different calibration states.
5. **ServiceMonitor over manual scrape.** Integrates with OpenShift's built-in Prometheus without requiring cluster-level config.
6. **Aggressive HPA scale-up, conservative scale-down.** 0s stabilization on scale-up (burst fast), 300s on scale-down (avoid flapping).
7. **NetworkPolicy ingress restrictions.** Allow only: namespaces tagged `kavara.ai/tier: client`, openshift-monitoring (metrics), openshift-ingress (the Route). Default-deny.
8. **Non-root UID 1001.** Satisfies OpenShift's default restricted SCC — no privileged SCC binding required.
9. **Single uvicorn worker per pod.** SOR in-process state (hardware detection cache, policy tables) is per-process. Scale via Deployment replicas, not uvicorn `--workers`.
10. **RuntimeClass for TDX commented, not removed.** When OpenShift sandboxed containers + TDX is GA on the target cluster, uncomment the `RuntimeClass: kata-cc-tdx` resource and the `runtimeClassName` reference in the Deployment. No other changes needed.

## Calibration — where the policy numbers come from

The `THRESHOLD_PROFILES` and `VENUE_POLICIES` tables in `sor_router.py` carry values calibrated by Kavara's benchmark harness on Intel Granite Rapids inside an Intel TDX Trust Domain (host: `ny5ulysses01`, guest: `kirk-td`). Specifically:

- **`'gnr-tdx': {'fused_max': 20, 'cpu_max': 800}`** — Intel's SPR-tuned `cpu_max=500` is measurably wrong on GNR+TDX. Our A/B experiment (`ONEDNN_MAX_CPU_ISA=AVX512_CORE` toggled) showed that AMX engagement below ~N=800 is counterproductive on GNR+TDX: kernel JIT + tile register setup cost exceeds the benefit at small sizes. Calibrated 2026-04-19.
- **Stride-2 placement venue (`amx-stride2-32`)** — E2 calibration sweep across five OMP_PLACES variants at every N from 5 to 4000 identified stride-2 with 32 threads as the universal optimum. Beats 64-thread packed configurations by 7–19% depending on N. Calibrated 2026-04-20.
- **AMX contribution decomposition** — A separate E5 experiment isolated "BF16 BRGEMM without AMX tiles" as a middle variant. Finding: BF16 BRGEMM alone captures ~80% of AMX's gain up to N=2000; AMX tiles add a further ~12-20% at N≥2000, and dominate at N=4000 (+20% over BF16-BRGEMM). So the marketed "3.77× AMX speedup" decomposes roughly as ~3× from the BRGEMM algorithm + ~1.2× from AMX tile instructions specifically.

These are the empirical inputs; the SOR's job is to encode them as deterministic routing.

## Architecture questions worth confirming with Red Hat

1. **OpenShift sandboxed containers + Intel TDX — GA timeline.** Which OpenShift version ships `kata-cc-tdx` or an equivalent RuntimeClass as supported rather than tech-preview?
2. **Node Feature Discovery.** Does NFD publish `AMX_BF16`, `AMX_TILE`, `AMX_INT8`, `TDX_GUEST` CPUID labels out of the box, or does it require a custom rule set?
3. **Topology Manager + Kata.** Is static `single-numa-node` topology policy supported for confidential pods, or does Kata break NUMA pinning today? This is load-bearing for Path 3.
4. **GPU + confidential-compute.** What's the current status of NVIDIA Confidential Computing (H100+) under OpenShift? scotty-gpu is our GPU venue candidate; we'd need routable confidential GPU pods to productize.
5. **OpenShift AI catalog.** Is there a path to publish KIRK (served via this SOR) as a first-party inference runtime in the OpenShift AI / Red Hat OpenShift AI catalog?
6. **Operator SDK precedent.** Any examples of a domain-specific routing / dispatch Operator (NVIDIA GPU Operator, Intel Device Plugins Operator) that a "Kavara SOR Operator" could model itself after?

## Related internal Kavara docs (not in this repo)

These links lead back to Kavara's private Notion / Confluence for context on the broader platform; they provide depth for conversations but are not required to run what's in this repo.

- **Workload-to-CPU Calculus** — the theoretical derivation for why certain configurations win at certain matrix sizes (working set vs cache hierarchy, dispatch cost vs thread count, etc.).
- **AMX Contribution on GNR+TDX (Quantified)** — the first-known isolated measurement of AMX contribution on Granite Rapids inside a Trust Domain.
- **Scheduled-Agent Architecture for the Calibration Loop** — how we automate calibration: offline runner agent + policy designer agent + consistency watcher + online deterministic router. This repo implements the "online deterministic router" component.

## Licensing

Apache License 2.0. See [`LICENSE`](./LICENSE).

## Contact

This reference repository is maintained by Kavara's platform engineering team. For architectural conversations, contact John Edge (`john.edge@kavara.ai`).
