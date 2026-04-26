# Inventory + connect: pick the right host in `office-of-cto-491318`

This replaces fresh provisioning. `office-of-cto-491318` already contains
the Kavara GCP fleet including an existing OpenShift build. The plan is
to have Claude Code enumerate what's running, exclude the air-gapped
hosts, pick the right target, and authenticate `oc` against it.

## Skip list (air-gapped, do not touch)

- **`scotty-gpu`** — model-IP vault. Per project memory: "air-gapped
  model-IP vault + future GPU compute venue." Never read its source,
  never deploy against it, never authenticate into it for this demo.

## Candidate list (safe for the demo)

Anything else in the project *may* be suitable. The known-good
demo-adjacent hosts per project memory are:

- **`tdx-amx-node-octo`** — GCP SPR TDX VM. Already has TDX attestation
  captured, good fit for the confidential consumer role.
- **`amd-sevsnp-benchmark`** — AMD SEV-SNP VM. Viable fallback if we
  want the SEV-SNP substrate demonstrated alongside TDX.
- The OpenShift cluster VMs already built in this project.

## Claude Code session \u2014 commands to run

Open a Claude Code session in this directory. Feed it this document and
ask it to work through the steps. Each block is a single command or
tight sequence; Claude Code should run, interpret, and move on.

### Step A \u2014 confirm we're in the right project

```bash
gcloud config set project office-of-cto-491318
gcloud config get-value project
gcloud auth list
```

Expected: project = `office-of-cto-491318`, an authenticated user.

### Step B \u2014 enumerate all VMs, excluding scotty-gpu

```bash
gcloud compute instances list \
  --format="table(name,zone.basename(),machineType.basename(),status,labels.list():label=LABELS)" \
  --filter="name!=scotty-gpu"
```

Claude Code should read the output and look for:

- An **OpenShift cluster** (VMs with names like `<clustername>-master-*` /
  `<clustername>-worker-*`, or labels `kubernetes-io-cluster-*`).
- VMs with `confidentialComputeType` set (TDX or SEV-SNP).
- VMs currently in `RUNNING` status.

### Step C \u2014 find the OpenShift cluster(s)

```bash
# Cluster VMs typically carry a kubernetes-io-cluster label.
gcloud compute instances list \
  --format="value(name,labels.list())" \
  --filter="name!=scotty-gpu AND labels.kubernetes-io-cluster-*=owned"

# OpenShift installer also tags VMs with an infraID label. Inspect any VM
# that looks cluster-adjacent.
INSTANCE_NAME="<pick a candidate>"
ZONE="<its zone>"
gcloud compute instances describe "${INSTANCE_NAME}" --zone="${ZONE}" \
  --format="yaml(labels,metadata,tags,confidentialInstanceConfig)"
```

### Step D \u2014 locate the API endpoint

```bash
# If the cluster has a managed router / load balancer it'll be in Cloud DNS.
gcloud dns managed-zones list
gcloud dns record-sets list --zone=<zone-name> | grep -E 'api\.|console\.'

# Or check the forwarding rules / target pools in front of the master set.
gcloud compute forwarding-rules list \
  --filter="description~cluster OR name~api"
gcloud compute target-pools list
```

### Step E \u2014 authenticate `oc` to the existing cluster

```bash
# Option 1: use the stored kubeconfig from whoever installed the cluster.
#          Ask JE where the auth/kubeconfig file lives; typically next to
#          the install-config.yaml used at install time.
export KUBECONFIG=/path/to/existing/install/auth/kubeconfig
oc whoami
oc get nodes -o wide

# Option 2: if no kubeconfig on hand, grab the API URL from Step D and
#          log in with credentials.
oc login https://api.<clustername>.<basedomain>:6443 -u kubeadmin -p <pw>
```

### Step F \u2014 verify the cluster has what the demo needs

```bash
# TDX-capable nodes?
oc get nodes -L feature.node.kubernetes.io/cpu-cpuid.TDX_GUEST \
             -L feature.node.kubernetes.io/cpu-cpuid.AMX_BF16

# OSC operator installed and kata-cc-tdx RuntimeClass present?
oc get csv -A | grep -Ei 'sandboxed|amq-streams|nfd|gitops'
oc get runtimeclass | grep -E 'kata|tdx'

# Enough free capacity for 2 consumer replicas at 32 CPU / 16 Gi each?
oc adm top nodes
oc describe nodes | grep -E 'Allocatable|Allocated resources' -A 5
```

### Step G \u2014 decide

Claude Code should emit a short report at this point:

```
Project:              office-of-cto-491318
Cluster chosen:       <clustername>
API endpoint:         https://api.<...>:6443
OCP version:          4.21.x
TDX-capable workers:  N out of M
OSC operator:         installed / not installed
AMQ Streams operator: installed / not installed
NFD operator:         installed / not installed
GitOps operator:      installed / not installed
Free capacity:        <enough / need to scale out>
Recommendation:       proceed / install missing operators / scale workers
```

If operators are missing, run the operator-install block from
`provision.md` Step 4. If node count is insufficient, scale the worker
MachineSet:

```bash
oc -n openshift-machine-api get machineset
oc -n openshift-machine-api scale machineset <name> --replicas=3
```

### Step H \u2014 hand off to the deployment runbook

Point Claude Code at [`../DEPLOY_WITH_CLAUDE_CODE.md`](../DEPLOY_WITH_CLAUDE_CODE.md)
and resume from Step 1 (namespace, secrets, RBAC).

## If the existing cluster is not the right fit

If what's there is too small, out of date, or shaped wrong for this
workload, fall back to provisioning a fresh cluster using the backup
config at [`install-config.yaml`](install-config.yaml). Keep the
**project** set to `office-of-cto-491318` (per JE) but add a cluster
`name:` that's distinct from anything already running.
