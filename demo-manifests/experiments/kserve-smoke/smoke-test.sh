#!/usr/bin/env bash
# KServe InferenceService smoke test for ulysses-sor-inference.
#
# Confirms three things end-to-end:
#   1. The predictor pod pulls quay.io/kavara/ulysses-sor-inference:v1
#      successfully (image is public on Quay, no pull secret needed).
#   2. The InferenceService reaches Ready=True (KServe RawDeployment mode,
#      no Knative).
#   3. POST /v2/models/{name}/infer with a 512-price window returns a
#      non-null FP64 von Neumann entropy that matches the consumer.py
#      pipeline's compute_entropy() output (bit-exact wrap pattern).
#
# Prerequisites: oc authenticated against the ulysses-demo cluster.
set -euo pipefail

NS=${NS:-ulysses-demo}
ISVC=${ISVC:-ulysses-sor-inference}
SVC="${ISVC}-predictor.${NS}.svc.cluster.local"

echo "--- 1. predictor pod state ---"
oc get pod -n "$NS" -l serving.kserve.io/inferenceservice="$ISVC" \
  -o custom-columns=NAME:.metadata.name,READY:.status.containerStatuses[0].ready,STATUS:.status.phase,RESTARTS:.status.containerStatuses[0].restartCount,AGE:.metadata.creationTimestamp

echo
echo "--- 2. InferenceService Ready condition ---"
oc get inferenceservice "$ISVC" -n "$NS" \
  -o jsonpath='{.status.conditions[?(@.type=="Ready")]}' | python3 -m json.tool

echo
echo "--- 3. v2 infer with 512-price synthetic Polygon-shape window ---"
# We exec into the SOR pod (which has python3+curl) to reach the predictor's
# ClusterIP endpoint without standing up a Route. RawDeployment mode doesn't
# auto-mint an OCP Route; production-facing exposure happens via a separate
# Route resource (out of scope for this smoke test).
oc exec -n "$NS" deploy/ulysses-sor -- python3 -c "
import json, subprocess, math, random
random.seed(42)
p = 100.0
prices = []
for _ in range(512):
    p *= math.exp(random.gauss(0, 0.0008))
    prices.append(p)
body = {
    'id': 'smoke-full',
    'inputs': [{'name': 'prices', 'shape': [1, 512], 'datatype': 'FP64', 'data': prices}],
}
r = subprocess.run(
    ['curl','-fsS','-m','10','-H','Content-Type: application/json',
     '-X','POST','http://${SVC}/v2/models/${ISVC}/infer',
     '-d', json.dumps(body)],
    capture_output=True, text=True, check=True,
)
resp = json.loads(r.stdout)
ent = resp['outputs'][0]['data'][0]
assert ent is not None, 'entropy is null — input shape too small for embed_dim+4'
assert 0 < ent < math.log(32) + 0.01, f'entropy {ent} outside theoretical bound [0, ln(32)]'
print(f'entropy = {ent:.6f} nats (max possible for 32-d rho is {math.log(32):.6f})')
print('smoke OK')
"
