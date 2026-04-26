# Driving the Ulysses demo deployment with Claude Code

Short answer: **yes, and this is actually a nice story to tell Red Hat.**

Claude Code runs locally on the operator's workstation with access to a bash
shell and file-editing tools. That is exactly the surface area a GitOps
deployment of this platform needs: an `oc` CLI authenticated to the target
cluster, a `podman` for the image build, a Git client for the ArgoCD source
of truth, and a file editor for the manifests. Every step below is a
command Claude Code can propose, run, interpret, and iterate on, with a
human in the loop for approval of state-changing actions.

Why this is a narrative worth keeping: Red Hat has been leaning into
AI-assisted operations (Ansible Lightspeed, OpenShift Lightspeed). A
Kavara-branded deployment driven by Claude Code is the logical extension at
a higher level \u2014 *natural-language GitOps*. If we record the deployment
session, we get keynote footage of \"Kavara deploys confidential financial
inference on OpenShift in under an hour via Claude Code driving ArgoCD.\"

## What Claude Code is good at here

| Task | How |
|---|---|
| Running `oc` / `kubectl` / `podman` / `gcloud` | Bash tool, streaming output, interprets errors |
| Editing any manifest in this directory | Edit / Write tools |
| Diagnosing pod failures | `oc describe`, `oc logs`, matches symptoms to known patterns |
| Verifying TDX attestation | Runs `oc exec ... -- dmesg \| grep tdx`, captures output |
| Committing + pushing manifest changes | `git add / commit / push`, drives ArgoCD reconcile |
| Watching HPA behaviour under load | `oc get hpa -w`, explains what it's seeing |

## What Claude Code shouldn't do solo

| Task | Why |
|---|---|
| Approve MFA prompts for `oc login`, `podman login`, `gcloud auth` | Human-in-the-loop security control |
| Commit IP-sensitive code (`super_kirk*`, `rapid_kirk*`) | Project memory air-gap rule |
| Authorize spend > $100 | Human judgment on budget |
| Auto-send external email to Mike / Jonathan | Per-project rule: JE is sole external contact |

## The deployment walkthrough (human-guided, Claude-executed)

Run these as a session in the project folder, with an authenticated `oc` /
`podman` in the environment. Claude Code proposes each command, waits for
approval, runs it, and interprets output before proceeding.

### 0. Preflight

**Cluster target:** existing OpenShift build in the GCP project
`office-of-cto-491318`. Do **not** touch `scotty-gpu` (air-gapped
model-IP vault). Claude Code should inventory the fleet first and pick
a suitable host; see [`cluster/inventory-and-connect.md`](cluster/inventory-and-connect.md)
for the explicit commands.

```bash
# Point at the right GCP project
gcloud config set project office-of-cto-491318
gcloud compute instances list --filter="name!=scotty-gpu" \
  --format="table(name,zone.basename(),machineType.basename(),status)"

# Once you've picked / located the existing cluster, authenticate
export KUBECONFIG=/path/to/existing/auth/kubeconfig   # ask JE for the path
oc version
oc whoami
oc get nodes -o wide

# Confirm required operators exist (install via cluster/inventory-and-connect.md
# Step G if missing)
oc get csv -A | grep -Ei 'sandboxed|amq-streams|nfd|gitops'

# Confirm at least one worker has TDX + AMX labels from NFD
oc get nodes -L feature.node.kubernetes.io/cpu-cpuid.TDX_GUEST \
             -L feature.node.kubernetes.io/cpu-cpuid.AMX_BF16
```

### 1. Namespace, secrets, RBAC

```bash
oc apply -f 01-namespace.yaml

# Polygon API key (JE provides the value)
oc -n ulysses-demo create secret generic polygon-api-key \\
  --from-literal=POLYGON_API_KEY=\"${POLYGON_API_KEY}\"

# Optional: image pull secret if ulysses-sor is in a private registry
# oc -n ulysses-demo create secret docker-registry quay-pull \\
#   --docker-server=quay.io --docker-username=... --docker-password=...
# oc -n ulysses-demo secrets link ulysses-sor quay-pull --for=pull
```

### 2. Build and push the consumer image

```bash
cd image
podman login quay.io
./build.sh            # defaults: quay.io/kavara/ulysses-sor:demo-v1
cd ..
```

### 3. Kafka cluster + topics (Strimzi)

```bash
oc apply -f 02-kafka-cluster.yaml
# Wait for readiness \u2014 Strimzi will create ~10 resources
oc -n ulysses-demo wait kafka/kafka-cluster --for=condition=Ready --timeout=10m
oc -n ulysses-demo get kafkatopic
oc -n ulysses-demo get kafkauser
```

### 4. Polygon ingress

```bash
oc apply -f 03-polygon-ingress.yaml
# Drop 04-polygon-ingress.py into the src ConfigMap (demo shortcut):
oc -n ulysses-demo create configmap polygon-ingress-src \\
  --from-file=ingress.py=04-polygon-ingress.py \\
  --dry-run=client -o yaml | oc apply -f -
# Watch logs until first produce message appears
oc -n ulysses-demo logs -f deploy/polygon-ingress
```

### 5. Ulysses SOR consumer (confidential)

```bash
oc apply -f 05-ulysses-consumer.yaml
oc -n ulysses-demo wait deploy/ulysses-sor --for=condition=Available --timeout=5m

# Attestation proof \u2014 this is the customer-facing evidence screenshot
POD=$(oc -n ulysses-demo get pod -l app=ulysses-sor -o name | head -1)
oc -n ulysses-demo exec \"${POD}\" -- dmesg | grep -i tdx
oc -n ulysses-demo exec \"${POD}\" -- env | grep OMP_

# Watch consumer logs confirm it's detecting hardware_id correctly
oc -n ulysses-demo logs -f deploy/ulysses-sor | grep hardware_id
```

### 6. Observability

```bash
oc apply -f 06-servicemonitor.yaml
oc apply -f 08-grafana-dashboard.yaml

# Verify Prometheus is scraping
oc -n openshift-monitoring exec -c prometheus prometheus-k8s-0 -- \\
  wget -qO- 'http://localhost:9090/api/v1/targets' | \\
  jq '.data.activeTargets[] | select(.labels.namespace==\"ulysses-demo\")'

# Confirm entropy metric is being produced
oc -n openshift-monitoring exec -c prometheus prometheus-k8s-0 -- \\
  wget -qO- 'http://localhost:9090/api/v1/query?query=ulysses_entropy_score' | jq
```

### 7. GitOps handover

```bash
# Point ArgoCD at the repo (after committing this directory somewhere)
oc apply -f 07-argocd-app.yaml
oc -n openshift-gitops get applications ulysses-demo

# From now on, any change to the YAML in Git auto-reconciles to the cluster
```

### 8. End-to-end verification

```bash
# Consume entropy messages \u2014 this is the \"it's alive\" moment
oc -n ulysses-demo exec -it kafka-cluster-broker-0 -- \\
  bin/kafka-console-consumer.sh \\
    --bootstrap-server localhost:9092 \\
    --topic ulysses.entropy.equities \\
    --max-messages 20
```

### 9. Replay-mode toggle (for keynote recording)

```bash
# Switch ingress to replay a known high-signal date
oc -n ulysses-demo patch configmap polygon-ingress-config --type merge \\
  -p '{\"data\":{\"MODE\":\"replay\",\"REPLAY_DATE\":\"2024-08-05\"}}'
oc -n ulysses-demo rollout restart deploy/polygon-ingress

# Grafana will show entropy spikes lining up with the JPY carry-trade unwind.
# That's the screenshot we want on the keynote slide.
```

## What a Claude Code session looks like in practice

1. **Operator opens a Claude Code session** in `~/Documents/Claude/Projects/Project Redhat/`.
2. Operator: \"Deploy this to the cluster I'm logged into\".
3. Claude proposes step 0 (preflight), operator approves, Claude runs, Claude reads output, reports any issues.
4. Claude walks through steps 1\u20139 in sequence, pausing at each state-changing action for approval.
5. If step 5 fails (e.g. OSC operator hasn't registered the `kata-cc-tdx` RuntimeClass yet), Claude diagnoses via `oc describe`, proposes the fix (install the operator, wait, retry), runs it, continues.
6. End of session: working demo with attestation logs captured, Grafana dashboard live.

**Expected wall-clock:** 60\u201390 minutes end to end assuming operators are already installed. Claude Code does most of the typing; operator provides judgment and MFA approvals.

## If we want this as the Red Hat Summit keynote story

Record the session from step 0. Resulting artifact: 45-minute time-lapse
showing natural-language deployment of a confidential-inference workload on
OpenShift, ending with live entropy streaming on a dashboard with TDX
attestation proof in the log pane. That's a keynote-grade demo that no
other ISV can currently reproduce \u2014 because no other ISV has the
CPU-native, messaging-oriented, confidential-by-default pattern that maps
this cleanly to natural-language operation.

## Handoff note for Jarett

Production replacement of `compute_entropy` in `image/consumer.py` is a
one-function swap. The rest of the pipeline (Kafka consumer, per-ticker
window, emission loop, Prometheus metrics, graceful shutdown) is reusable
as-is. The production image becomes `ulysses-sor:prod-vN` with the real
normalized-EBM pipeline imported from the air-gapped module; no other
manifest changes.
