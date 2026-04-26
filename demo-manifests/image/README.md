# `ulysses-sor:demo-v1` container image

Consumer image for the Polygon → AMQ Streams → Ulysses demo. Referenced by
`05-ulysses-consumer.yaml` in the parent directory.

## What it does

Consumes `market.equities.trades` from AMQ Streams, maintains a per-ticker
rolling window, and every `WINDOW_SECONDS` (default 12s) computes the
**von Neumann entropy of a delay-coordinate density matrix** built from the
log-return series. Emits results to `ulysses.entropy.equities`. Exposes
Prometheus metrics on `:9100`.

This is a **public-safe stub**. The real SOR + Ulysses normalized-EBM
pipeline is air-gapped per Kavara policy. The stub is a legitimate
density-matrix entropy calculation — it will move meaningfully on regime
shifts — but it does not incorporate Joel's partition-function
normalization that makes Ulysses's output cross-comparable across tenants.
Swap `compute_entropy` for the real pipeline in production.

## Files

| File | What |
|---|---|
| `Dockerfile` | UBI9 Python 3.11 base, rootless, non-cached pip install |
| `requirements.txt` | confluent-kafka, numpy, scipy, prometheus-client, json logger |
| `consumer.py` | Entrypoint — Kafka consumer + per-ticker windows + entropy |
| `healthz.py` | Stand-alone probe helper (not used at runtime normally) |
| `build.sh` | Builds and pushes via podman (or docker) |
| `.dockerignore` | Keeps build context small |

## Build and push

```bash
# Log in to your registry first
podman login quay.io

# Defaults: quay.io/kavara/ulysses-sor:demo-v1, linux/amd64
./build.sh

# Or override:
REGISTRY=quay.io/johnedge TAG=demo-v2 ./build.sh
```

After push, make sure `05-ulysses-consumer.yaml` references the same tag:

```yaml
image: quay.io/kavara/ulysses-sor:demo-v1
```

If you pushed to a private registry, create an image pull secret and
reference it from the `ulysses-sor` ServiceAccount in `01-namespace.yaml`.

## Local smoke test (no Kafka, no OpenShift)

```bash
# Build
podman build -t ulysses-sor:local .

# Run against a local Kafka (assumes plaintext, no TLS)
podman run --rm -it \
  -e KAFKA_BOOTSTRAP=host.containers.internal:9092 \
  -e SECURITY_PROTOCOL=PLAINTEXT \
  -e TOPIC_TRADES_IN=market.equities.trades \
  -e TOPIC_ENTROPY_OUT=ulysses.entropy.equities \
  -e WINDOW_SECONDS=5 \
  -e MIN_SAMPLES=16 \
  -p 9100:9100 \
  ulysses-sor:local

# In another shell
curl -s localhost:9100/metrics | grep ulysses_entropy
```

## The entropy calculation (reference)

```
r   = log-returns over the last N samples
X   = sliding-window embed(r, D)           # N-D+1 rows × D cols
G   = X^T X                                 # D × D, Hermitian, PSD
ρ   = G / tr(G)                             # density matrix, tr(ρ)=1
λ_i = eigvalsh(ρ)                           # real, non-negative
S   = -Σ_i λ_i · ln(λ_i)   for λ_i > ε     # von Neumann entropy
```

Bounded `0 ≤ S ≤ ln D`. For `EMBED_DIM=32` the upper bound is `ln 32 ≈ 3.47`.
Flat-market regimes cluster at the low end; structural disorder pushes
toward the top. This is the *shape* of what Ulysses emits in production —
the real pipeline adds normalization and cross-ticker coupling so scores
are comparable across tenants and across days.

## Runtime environment

Matches the OpenMP calibration from the KIRK cross-platform study:

```
OMP_NUM_THREADS=32
OMP_PROC_BIND=close
OMP_PLACES=cores
```

These are baked into the Dockerfile as defaults and can be overridden via
the ConfigMap at deploy time without a rebuild.

The container is designed to run under `runtimeClassName: kata-cc-tdx`
(Intel TDX via OpenShift Sandboxed Containers). The AMX fast path engages
automatically via oneDNN when NumPy/SciPy detect the CPUID flags — no
code change between substrates.

## Image provenance and licensing

Base: `registry.access.redhat.com/ubi9/python-311`. UBI is redistributable;
the built image can be pushed to public Quay or private registries alike.
All Python dependencies are permissively licensed (BSD / Apache / MIT /
PSF). This image is Apache 2.0, consistent with the `tiberius-openshift`
reference repository.
