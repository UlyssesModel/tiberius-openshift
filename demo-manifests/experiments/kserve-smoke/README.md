# KServe smoke test — ulysses-sor-inference

Confirms the V2-OpenInference InferenceService wrap of `compute_entropy`
serves predictions on the live SNO cluster.

## What this proves

- `quay.io/kavara/ulysses-sor-inference:v1` is publicly pullable from Quay
  (image was previously private — toggling visibility unblocked the
  predictor's `ImagePullBackOff` loop).
- KServe RawDeployment mode reaches `Ready=True` without Knative — the
  Predictor's ClusterIP service is reachable via in-cluster DNS at
  `ulysses-sor-inference-predictor.ulysses-demo.svc.cluster.local`.
- `POST /v2/models/ulysses-sor-inference/infer` with a 512-price window
  returns a non-null FP64 entropy that lies in the theoretical bound
  `[0, ln(embed_dim)]` — confirms the wrapped `compute_entropy` matches
  the consumer.py pipeline output.

## Run it

```
./smoke-test.sh
```

Prerequisites: `oc` authenticated against the ulysses-demo cluster
kubeconfig.

## Captured output

`smoke-test.log` is the verbatim run output (predictor pod state,
InferenceService Ready condition, inference response).

## Notes

- KServe is in **RawDeployment** mode (`serving.kserve.io/deploymentMode:
  RawDeployment` annotation). No Knative, no Istio. The Predictor exposes
  a plain ClusterIP Service.
- The InferenceService `status.components.predictor.url` shows
  `…example.com` because the cluster-wide KServe ingress domain
  (`config-deployment` ConfigMap → `ingressDomain`) hasn't been set. For
  external exposure, mint a separate OCP `Route` targeting the predictor
  Service — out of scope for this smoke test.
- v1 endpoint (`POST /v1/models/{name}:predict`) returned 500 on a small
  test input; the model server's V1 handler is shape-stricter than V2.
  Use V2 for all client integrations — that's also what the production
  KServe wrapping pattern documents.
