# Provisioning the Ulysses demo cluster on GCP

Target: a clean OpenShift 4.21.9+ cluster on GCP Compute Engine in
`us-east4` with TDX-capable Intel Sapphire Rapids workers, ready to
receive `oc apply -f demo-manifests/`.

## Prereqs checklist

- [ ] GCP project created (suggested name: `kavara-redhat-demo`).
      **Do not reuse `office-of-cto-491318`** — that is scotty-gpu's
      air-gapped IP vault.
- [ ] GCP free credits applied to the project (Billing > Credits).
- [ ] `gcloud` CLI authenticated, project set:
  ```bash
  gcloud auth login
  gcloud config set project kavara-redhat-demo
  ```
- [ ] Required APIs enabled (Claude Code can run these):
  ```bash
  gcloud services enable \
    compute.googleapis.com cloudresourcemanager.googleapis.com \
    dns.googleapis.com iam.googleapis.com iamcredentials.googleapis.com \
    serviceusage.googleapis.com storage-api.googleapis.com \
    storage-component.googleapis.com
  ```
- [ ] Cloud DNS zone for the cluster's `baseDomain` (e.g.
      `kavara-redhat-demo.kavara.ai`) — OR reuse an existing Kavara-owned
      zone.
- [ ] OpenShift installer binary downloaded from
      https://console.redhat.com/openshift/install/gcp/installer-provisioned.
- [ ] Red Hat pull secret from the same page.
- [ ] An SSH public key you want to seed onto the cluster nodes.
- [ ] A service account with Owner role in the GCP project, key JSON
      saved to `~/.gcp/osServiceAccount.json`.

## Steps

### 1. Confirm c3 TDX availability in us-east4

Google rolls TDX GA per region. Before committing, sanity check:

```bash
gcloud compute machine-types describe c3-highcpu-22 --zone=us-east4-a \
  --format="value(name,guestCpus,memoryMb)"
gcloud compute instances create tdx-probe \
  --zone=us-east4-a \
  --machine-type=c3-highcpu-22 \
  --confidential-compute-type=TDX \
  --maintenance-policy=TERMINATE \
  --image-family=rhel-9 --image-project=rhel-cloud
# If the command succeeds, TDX is available. Delete the probe:
gcloud compute instances delete tdx-probe --zone=us-east4-a
```

If TDX is not yet GA in us-east4 for c3, fall back to `us-central1` or
`us-east1` where GCP has had TDX longer. Update `install-config.yaml`
accordingly.

### 2. Fill in `install-config.yaml` secrets

Open `install-config.yaml`, replace:

- `pullSecret:` — the full JSON blob from console.redhat.com.
- `sshKey:` — your `~/.ssh/id_ed25519.pub` or equivalent.
- `baseDomain:` — the Cloud DNS zone you control.

Keep a backup copy of the completed config outside the install dir —
`openshift-install` consumes and deletes the file.

### 3. Run the installer

```bash
cd demo-manifests/cluster
openshift-install create cluster --dir=. --log-level=info
```

Expected wall clock: **30–40 minutes**. The installer creates VPC, subnets,
firewall rules, service accounts, 3 master VMs, 3 worker VMs, a router,
a registry, and bootstraps the cluster. On success it prints:

```
INFO Install complete!
INFO To access the cluster as the system:admin user when using 'oc', run
INFO     export KUBECONFIG=.../demo-manifests/cluster/auth/kubeconfig
INFO Access the OpenShift web-console here: https://console-openshift-console.apps.ulysses-demo.kavara-redhat-demo.kavara.ai
INFO Login to the console with user: "kubeadmin", and password: "..."
```

### 4. Install the operators the manifests need

```bash
export KUBECONFIG=$PWD/auth/kubeconfig
oc apply -f - <<'EOF'
---
apiVersion: operators.coreos.com/v1
kind: OperatorGroup
metadata:
  name: openshift-operators-global
  namespace: openshift-operators
spec: {}
---
apiVersion: operators.coreos.com/v1alpha1
kind: Subscription
metadata:
  name: sandboxed-containers-operator
  namespace: openshift-operators
spec:
  channel: stable
  installPlanApproval: Automatic
  name: sandboxed-containers-operator
  source: redhat-operators
  sourceNamespace: openshift-marketplace
---
apiVersion: operators.coreos.com/v1alpha1
kind: Subscription
metadata:
  name: amq-streams
  namespace: openshift-operators
spec:
  channel: stable
  installPlanApproval: Automatic
  name: amq-streams
  source: redhat-operators
  sourceNamespace: openshift-marketplace
---
apiVersion: operators.coreos.com/v1alpha1
kind: Subscription
metadata:
  name: nfd
  namespace: openshift-operators
spec:
  channel: stable
  installPlanApproval: Automatic
  name: nfd
  source: redhat-operators
  sourceNamespace: openshift-marketplace
---
apiVersion: operators.coreos.com/v1alpha1
kind: Subscription
metadata:
  name: openshift-gitops-operator
  namespace: openshift-operators
spec:
  channel: latest
  installPlanApproval: Automatic
  name: openshift-gitops-operator
  source: redhat-operators
  sourceNamespace: openshift-marketplace
EOF

# Wait for operators to install (~3-5 min)
oc get csv -A -w
```

### 5. Activate NFD and the Kata RuntimeClass

```bash
# NFD: tells OCP to label nodes with CPUID (AMX_BF16, TDX_GUEST, etc.).
oc apply -f - <<'EOF'
apiVersion: nfd.openshift.io/v1
kind: NodeFeatureDiscovery
metadata:
  name: nfd-instance
  namespace: openshift-operators
spec:
  operand:
    image: registry.redhat.io/openshift4/ose-node-feature-discovery:latest
EOF

# OSC: deploys the kata-cc-tdx RuntimeClass.
oc apply -f - <<'EOF'
apiVersion: kataconfiguration.openshift.io/v1
kind: KataConfig
metadata:
  name: cluster-kataconfig
spec:
  enablePeerPods: false
  # Limit kata to the 2 TDX worker nodes to avoid installing it on masters.
  kataConfigPoolSelector:
    matchLabels:
      feature.node.kubernetes.io/cpu-cpuid.TDX_GUEST: "true"
EOF

# Verify after a few minutes
oc get nodes -L feature.node.kubernetes.io/cpu-cpuid.TDX_GUEST,feature.node.kubernetes.io/cpu-cpuid.AMX_BF16
oc get runtimeclass | grep kata-cc
```

### 6. Hand off to the Claude Code deployment runbook

At this point the cluster is ready for `oc apply -f 01-...07-*.yaml`.
Switch to [`../DEPLOY_WITH_CLAUDE_CODE.md`](../DEPLOY_WITH_CLAUDE_CODE.md)
and drive the rest of the deployment from there.

## Teardown

```bash
cd demo-manifests/cluster
openshift-install destroy cluster --dir=. --log-level=info
```

Takes ~15 minutes. Verify nothing orphaned in GCP:

```bash
gcloud compute instances list
gcloud compute networks list
gcloud compute disks list
```

## Cost tracking

Set a budget alert in the GCP project at $1,500 (inside the $1k–$5k
experiment band, with headroom for a third week if the demo gets reused).

```bash
gcloud billing budgets create \
  --billing-account=BILLING_ACCOUNT_ID \
  --display-name="ulysses-demo-hard-cap" \
  --budget-amount=1500USD \
  --threshold-rule=percent=0.5,basis=current-spend \
  --threshold-rule=percent=0.9,basis=current-spend \
  --threshold-rule=percent=1.0,basis=current-spend
```
