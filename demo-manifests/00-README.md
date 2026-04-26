# Ulysses × OpenShift Capital-Markets Demo — Component Manifest

Reference deployment for the Polygon-fed Ulysses demo on OpenShift, as
specified in the Architecture Brief for Jonathan Keam (2026-04-23).

## What this deploys

```
Polygon.io WebSocket
     │
     ▼
┌─────────────────────────────┐
│  polygon-ingress pod        │   runtimeClass: runc (non-confidential)
│  (normalizes + produces)    │   namespace: ulysses-demo
└─────────┬───────────────────┘
          │
          ▼  AMQ Streams topic: market.equities.trades / .quotes
┌─────────────────────────────┐
│  AMQ Streams (Strimzi)      │   3-broker KRaft cluster
│  3 topics × 12 partitions   │   operator-managed
└─────────┬───────────────────┘
          │
          ▼
┌─────────────────────────────┐
│  ulysses-sor consumer       │   runtimeClass: kata-cc-tdx (TDX)
│  Deployment, consumer group │   replicas: 2
│  per-ticker rolling window  │   nfd-selected: gnr-tdx
│  SOR + Ulysses pipeline     │
└─────────┬───────────────────┘
          │
          ▼  AMQ Streams topic: ulysses.entropy.equities
┌─────────────────────────────┐
│  Prometheus ServiceMonitor  │
│  + Grafana dashboard        │
└─────────────────────────────┘
```

## Files

| File | What it does |
|---|---|
| `01-namespace.yaml` | Namespace + RBAC |
| `02-kafka-cluster.yaml` | Strimzi Kafka CR (3 brokers, KRaft) + the three topics |
| `03-polygon-ingress.yaml` | ConfigMap (API key + topic config) + Deployment + Service |
| `04-polygon-ingress.py` | Python producer script (baked into a ConfigMap for demo simplicity) |
| `05-ulysses-consumer.yaml` | Deployment with `runtimeClassName: kata-cc-tdx`, consumer group, NFD selector |
| `06-servicemonitor.yaml` | Prometheus ServiceMonitor for scraping entropy metrics |
| `07-argocd-app.yaml` | ArgoCD Application wrapping the lot, points at our Git repo |

## Prerequisites

- OpenShift 4.21.9+ cluster with TDX-capable workers.
- Operators installed (via OperatorHub, or included in an Argo App-of-Apps):
    - **AMQ Streams operator** (Strimzi) — any recent version.
    - **OpenShift Sandboxed Containers operator** (registers the
      `kata-cc-tdx` RuntimeClass).
    - **Node Feature Discovery operator** — labels nodes with CPUID.
    - **Prometheus operator** (shipped with OpenShift Monitoring).
    - **OpenShift GitOps operator** (ArgoCD).
- A Polygon.io API key (Developer $99/mo or Advanced $199/mo).
    Stored as a Kubernetes Secret via external-secrets or `oc create secret`.

## Deploy

```bash
# One-shot: install the manifests directly
oc apply -f 01-namespace.yaml
oc create secret generic polygon-api-key \
  --from-literal=POLYGON_API_KEY="${POLYGON_API_KEY}" \
  -n ulysses-demo
oc apply -f 02-kafka-cluster.yaml
oc apply -f 03-polygon-ingress.yaml
oc apply -f 05-ulysses-consumer.yaml
oc apply -f 06-servicemonitor.yaml

# Recommended: go through ArgoCD so everything is Git-tracked
oc apply -f 07-argocd-app.yaml
```

## Verify

```bash
# Kafka cluster up
oc -n ulysses-demo get kafka,kafkatopic

# Polygon producing
oc -n ulysses-demo logs deploy/polygon-ingress --tail=20

# TDX attestation on the consumer
oc -n ulysses-demo exec deploy/ulysses-sor -- dmesg | grep -i tdx

# Entropy topic flowing
oc -n ulysses-demo exec -it kafka-cluster-kafka-0 -- \
  bin/kafka-console-consumer.sh --bootstrap-server localhost:9092 \
  --topic ulysses.entropy.equities --max-messages 5
```

## Replay mode (for Summit keynote)

Flip the ConfigMap `polygon-ingress-config`:

```bash
oc -n ulysses-demo patch configmap polygon-ingress-config \
  --type merge -p '{"data":{"MODE":"replay","REPLAY_DATE":"2024-08-05"}}'
oc -n ulysses-demo rollout restart deploy/polygon-ingress
```

The producer switches from live WebSocket to flat-file replay of Polygon's
historical endpoint for the specified date. Same Kafka topics, same consumer,
deterministic on stage.

## Costs (two-week demo envelope)

- Polygon.io Developer: $99 × 0.5mo ≈ $50.
- GCP `c3-standard-22` (TDX-enabled), 2 workers + 1 broker host, spot pricing:
  roughly $0.50/hr × 3 × 24 × 14 ≈ $500.
- Total: **~$550** — inside the $300–600 envelope in the arch brief.
