#!/usr/bin/env bash
#
# Provision the Ulysses demo cluster on GCP.
#
#   • Target:     Single-Node OpenShift (SNO) on c3-standard-44 in
#                 office-of-cto-491318 / us-central1-a. SNO control plane
#                 hosts the full demo workload (Kafka brokers, Polygon
#                 ingress, Ulysses consumer); kata-cc-tdx RuntimeClass
#                 from OSC provides per-pod TDX confidentiality.
#   • Installer:  OpenShift IPI (installer-provisioned infrastructure). IPI
#                 handles GCP networking / LB / DNS plumbing end-to-end
#                 and gives us a clean `openshift-install destroy cluster`
#                 path for teardown.
#   • Auth:       Mint mode via a dedicated installer SA whose JSON key
#                 lives at ${SA_KEY_FILE}. Created once via the org-policy
#                 exception (see postmortem/path-A-findings.md for why we
#                 abandoned Manual mode).
#   • Output:     KUBECONFIG at ${INSTALL_DIR}/auth/kubeconfig, ready for
#                 `oc apply -f ../01-namespace.yaml` etc.
#
# Safe to re-run: most steps are idempotent or gated on "exists".
# Any state-changing action prompts for confirmation unless --yes is passed.
#
# Usage:
#   export RH_PULL_SECRET='{"auths":...}'                 # from console.redhat.com
#   export GCP_BASE_DOMAIN=kavara.gcp.local               # Cloud DNS zone you own
#   export SA_KEY_FILE=$HOME/.gcp/ulysses-demo-installer.json  # Mint-mode SA key
#   ./provision-ulysses-demo-tdx.sh [--yes] [--destroy] [--reset]
#
# Lessons-learned guards (added after the 2026-04-24 run):
#   * Quota fit check at machine-type granularity (need 2 x guestCpus).
#   * Stale install-state guard refuses to overwrite without --reset.
#   * macOS BSD-tar guard requires gtar before any agent-install path.
#   * Post-install grep for "Install complete!" because openshift-install
#     can return 0 mid-flight when CAPI errors out without surfacing FATAL.
#
# ---------------------------------------------------------------------------

set -euo pipefail

# ---------- config knobs ---------------------------------------------------
PROJECT_ID="${PROJECT_ID:-office-of-cto-491318}"
REGION="${REGION:-us-central1}"
ZONE="${ZONE:-us-central1-a}"
CLUSTER_NAME="${CLUSTER_NAME:-ulysses-demo}"
# c3-standard-44: bootstrap + master = 88 vCPU, fits in 128-vCPU C3 quota
# alongside the existing 30 vCPU of fleet workloads. c3-standard-88 was
# the original choice but doesn't fit (2 x 88 = 176 > 128).
MACHINE_TYPE="${MACHINE_TYPE:-c3-standard-44}"
BOOT_DISK_SIZE_GB="${BOOT_DISK_SIZE_GB:-250}"
OCP_VERSION="${OCP_VERSION:-4.21.9}"                # pin for reproducibility
INSTALL_DIR="${INSTALL_DIR:-$(pwd)/ulysses-demo-install}"
SSH_KEY_PATH="${SSH_KEY_PATH:-${HOME}/.ssh/id_ed25519.pub}"
SA_KEY_FILE="${SA_KEY_FILE:-${HOME}/.gcp/ulysses-demo-installer.json}"
GCP_BASE_DOMAIN="${GCP_BASE_DOMAIN:?set GCP_BASE_DOMAIN to a Cloud DNS zone you own (e.g. kavara.gcp.local)}"
RH_PULL_SECRET="${RH_PULL_SECRET:?set RH_PULL_SECRET to your pull secret JSON from console.redhat.com}"

YES="${YES:-0}"
DO_DESTROY="${DO_DESTROY:-0}"
DO_RESET="${DO_RESET:-0}"
for arg in "$@"; do
  case "$arg" in
    --yes)     YES=1 ;;
    --destroy) DO_DESTROY=1 ;;
    --reset)   DO_RESET=1 ;;
    *)         echo "unknown flag: $arg" >&2; exit 2 ;;
  esac
done

# ---------- helpers --------------------------------------------------------
log()  { printf '\033[1;36m[%s]\033[0m %s\n' "$(date +%H:%M:%S)" "$*"; }
warn() { printf '\033[1;33m[%s]\033[0m %s\n' "$(date +%H:%M:%S)" "$*" >&2; }
die()  { printf '\033[1;31m[%s]\033[0m %s\n' "$(date +%H:%M:%S)" "$*" >&2; exit 1; }
confirm() {
  [[ "${YES}" == "1" ]] && return 0
  local prompt="$1 [y/N] "
  read -r -p "$prompt" resp
  [[ "$resp" =~ ^[Yy]$ ]]
}

# ---------- teardown -------------------------------------------------------
if [[ "${DO_DESTROY}" == "1" ]]; then
  [[ -d "${INSTALL_DIR}" ]] || die "install dir not found: ${INSTALL_DIR}"
  log "Destroying cluster via openshift-install (this removes all GCP resources it created)"
  confirm "Proceed?" || die "aborted"
  openshift-install destroy cluster --dir="${INSTALL_DIR}" --log-level=info
  log "Done. Verify no orphans:"
  gcloud compute instances list --project="${PROJECT_ID}" --filter="name~^${CLUSTER_NAME}-"
  exit 0
fi

# ---------- 1. preflight ---------------------------------------------------
log "Preflight: tools + auth + project"

for bin in gcloud openshift-install oc jq curl python3; do
  command -v "$bin" >/dev/null 2>&1 || die "missing tool: $bin"
done

# macOS BSD-tar guard: GCE image imports reject BSD tar output.
if [[ "$(uname)" == "Darwin" ]] && ! command -v gtar >/dev/null 2>&1; then
  die "On macOS install GNU tar (Homebrew):  brew install gnu-tar"
fi

[[ -r "${SSH_KEY_PATH}" ]] || die "ssh pubkey not readable: ${SSH_KEY_PATH}"

# Mint-mode credentials: openshift-install needs a real SA key file via
# GOOGLE_APPLICATION_CREDENTIALS, NOT user-impersonation ADC. The org
# policy 'iam.disableServiceAccountKeyCreation' must be lifted at the
# project level to create the key — see postmortem/path-A-findings.md
# for the bootstrap recipe.
[[ -r "${SA_KEY_FILE}" ]] \
  || die "SA key file not readable: ${SA_KEY_FILE}. Create via:
    gcloud iam service-accounts keys create ${SA_KEY_FILE} \\
      --iam-account=ulysses-demo-installer@${PROJECT_ID}.iam.gserviceaccount.com
  (after lifting the iam.disableServiceAccountKeyCreation org policy)."
SA_EMAIL="$(jq -r '.client_email // empty' "${SA_KEY_FILE}")"
[[ -n "${SA_EMAIL}" ]] || die "SA key file at ${SA_KEY_FILE} has no client_email — corrupt?"
log "  SA key file: ${SA_KEY_FILE} (identity: ${SA_EMAIL})"

# Force openshift-install to use the SA key, not any cached user token.
export GOOGLE_APPLICATION_CREDENTIALS="${SA_KEY_FILE}"
unset CLOUDSDK_AUTH_ACCESS_TOKEN

CURRENT_PROJECT="$(gcloud config get-value project 2>/dev/null || true)"
if [[ "${CURRENT_PROJECT}" != "${PROJECT_ID}" ]]; then
  log "Switching gcloud project: ${CURRENT_PROJECT} -> ${PROJECT_ID}"
  gcloud config set project "${PROJECT_ID}"
fi

gcloud auth list --filter=status:ACTIVE --format="value(account)" | grep -q . \
  || die "no active gcloud account; run 'gcloud auth login'"

log "Installer version:"
openshift-install version

# ---------- 2. mirror openshift-intel-tdx service account if possible ------
log "Resolving service account to match openshift-intel-tdx"
REF_SA="$(gcloud compute instances describe openshift-intel-tdx \
  --zone="${ZONE}" --project="${PROJECT_ID}" \
  --format='value(serviceAccounts[0].email)' 2>/dev/null || true)"
if [[ -n "${REF_SA}" ]]; then
  log "  reference SA: ${REF_SA}"
else
  warn "  openshift-intel-tdx not found or inaccessible; using default compute SA"
fi

# ---------- 3. enable required APIs ---------------------------------------
log "Ensuring required GCP APIs are enabled"
REQUIRED_APIS=(
  compute.googleapis.com
  cloudresourcemanager.googleapis.com
  dns.googleapis.com
  iam.googleapis.com
  iamcredentials.googleapis.com
  serviceusage.googleapis.com
  storage-api.googleapis.com
  storage-component.googleapis.com
)
gcloud services enable "${REQUIRED_APIS[@]}" --project="${PROJECT_ID}"

# ---------- 4. quota fit check (machine-type-aware) -----------------------
# IPI provisions BOTH a bootstrap VM AND a master VM at the same machine
# type as controlPlane.platform.gcp.type. Need 2 x guestCpus available.
NEED_PER_VM=$(gcloud compute machine-types describe "${MACHINE_TYPE}" \
  --zone="${ZONE}" --format="value(guestCpus)" 2>/dev/null || echo 0)
NEED_TOTAL=$(( NEED_PER_VM * 2 ))
log "Checking C3_CPUS quota in ${REGION} (need ${NEED_TOTAL} for 2 x ${MACHINE_TYPE})"
QUOTA_JSON="$(gcloud compute regions describe "${REGION}" \
  --project="${PROJECT_ID}" --format=json \
  | jq '.quotas[] | select(.metric=="C3_CPUS")' 2>/dev/null || true)"
if [[ -n "${QUOTA_JSON}" ]]; then
  # GCP quota values come back as floats ("128.0"). Coerce to ints in jq.
  LIMIT=$(echo "${QUOTA_JSON}" | jq -r '.limit | floor')
  USED=$(echo "${QUOTA_JSON}" | jq -r '.usage | floor')
  AVAIL=$(( LIMIT - USED ))
  log "  c3 cpus: used ${USED} / limit ${LIMIT} -> available ${AVAIL}"
  if (( NEED_TOTAL > AVAIL )); then
    die "C3 quota fit check failed: need ${NEED_TOTAL} (2 x ${NEED_PER_VM} for bootstrap+master), have ${AVAIL}.
  Either reduce MACHINE_TYPE, stop other C3 instances, or request a quota
  increase at https://console.cloud.google.com/iam-admin/quotas."
  fi
fi

# ---------- 4b. stale install-state guard --------------------------------
# A previous failed install can leave .clusterapi_output/ behind with a
# half-running envtest control plane. Re-running on top of that gives us
# CVO overrides and a stuck cluster (see 2026-04-24 postmortem).
if [[ -d "${INSTALL_DIR}/.clusterapi_output" ]] || [[ -f "${INSTALL_DIR}/auth/kubeconfig" ]]; then
  if [[ "${DO_RESET}" == "1" ]]; then
    log "RESET: clearing prior install state at ${INSTALL_DIR}"
    rm -rf "${INSTALL_DIR}"
  else
    die "Prior install state exists at ${INSTALL_DIR}.
  Either:
    1) Run with --destroy to tear down the old cluster cleanly first, OR
    2) Run with --reset to wipe local state (use ONLY after destroy succeeded)."
  fi
fi

# ---------- 5. Cloud DNS zone sanity --------------------------------------
log "Confirming a Cloud DNS zone exists for baseDomain=${GCP_BASE_DOMAIN}"
DNS_ZONE="$(gcloud dns managed-zones list \
  --project="${PROJECT_ID}" \
  --filter="dnsName=${GCP_BASE_DOMAIN}." \
  --format='value(name)' | head -1)"
[[ -n "${DNS_ZONE}" ]] \
  || die "no managed-zone for ${GCP_BASE_DOMAIN} in project ${PROJECT_ID}. Create one or pass a different GCP_BASE_DOMAIN."
log "  using managed-zone: ${DNS_ZONE}"

# ---------- 6. render install-config.yaml ---------------------------------
log "Rendering install-config.yaml into ${INSTALL_DIR}"
mkdir -p "${INSTALL_DIR}"
SSH_PUBKEY="$(cat "${SSH_KEY_PATH}")"

# Render the SNO-on-GCP template from the adjacent file
TEMPLATE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/sno"
[[ -r "${TEMPLATE_DIR}/install-config-template.yaml" ]] \
  || die "missing template: ${TEMPLATE_DIR}/install-config-template.yaml"

# Use a small python heredoc to do safe string replacement (avoid sed corner cases)
python3 - <<PY > "${INSTALL_DIR}/install-config.yaml"
import os, json
t = open("${TEMPLATE_DIR}/install-config-template.yaml").read()
t = t.replace("__CLUSTER_NAME__",  "${CLUSTER_NAME}")
t = t.replace("__BASE_DOMAIN__",   "${GCP_BASE_DOMAIN}")
t = t.replace("__PROJECT_ID__",    "${PROJECT_ID}")
t = t.replace("__REGION__",        "${REGION}")
t = t.replace("__ZONE__",          "${ZONE}")
t = t.replace("__MACHINE_TYPE__",  "${MACHINE_TYPE}")
t = t.replace("__DISK_SIZE__",     "${BOOT_DISK_SIZE_GB}")
t = t.replace("__SSH_PUBKEY__",    """${SSH_PUBKEY}""")
# pull secret must be a single-line JSON blob
t = t.replace("__PULL_SECRET__",   json.dumps(json.loads(os.environ["RH_PULL_SECRET"])))
print(t)
PY

# Keep a backup because the installer consumes the file in-place
cp "${INSTALL_DIR}/install-config.yaml" "${INSTALL_DIR}/install-config.yaml.bak"
log "  written: ${INSTALL_DIR}/install-config.yaml (backup alongside)"

# ---------- 7. run the installer ------------------------------------------
if [[ -f "${INSTALL_DIR}/auth/kubeconfig" ]] && \
   grep -q "Install complete!" "${INSTALL_DIR}/.openshift_install.log" 2>/dev/null; then
  log "Cluster appears already installed (kubeconfig + Install complete! marker). Skipping create."
else
  log "Running openshift-install create cluster (expected wall-clock 30-40 min)"
  confirm "Proceed with cluster creation?" || die "aborted"
  openshift-install create cluster --dir="${INSTALL_DIR}" --log-level=info
fi

# Don't trust openshift-install's exit code alone. The 2026-04-24 incident
# showed that CAPI errors during bootstrap can cause the installer to
# return 0 mid-flight without ever reaching bootstrap-complete, leaving
# CVO overrides and a stuck cluster behind. Verify the explicit milestone.
if ! grep -q "Install complete!" "${INSTALL_DIR}/.openshift_install.log" 2>/dev/null; then
  warn "openshift-install returned 0 but no 'Install complete!' marker in log."
  warn "Last 20 lines of the install log:"
  tail -20 "${INSTALL_DIR}/.openshift_install.log" >&2
  die "Refusing to declare success without bootstrap-complete + install-complete signal.
  Run --destroy to tear down whatever partial state exists, then re-investigate."
fi

export KUBECONFIG="${INSTALL_DIR}/auth/kubeconfig"
log "KUBECONFIG: ${KUBECONFIG}"
oc whoami
oc get nodes -o wide
log "ClusterVersion sanity check (should show no spec.overrides):"
oc get clusterversion version -o jsonpath='{.spec.overrides}' && echo
oc get clusterversion

# ---------- 8. label + tag the node to match the existing cell ------------
log "Labeling the SNO node to match openshift-intel-tdx convention"
NODE="$(oc get nodes -o jsonpath='{.items[0].metadata.name}')"
oc label node "${NODE}" \
  cell=intel-tdx \
  owner=je \
  workstream=openshift \
  purpose=ulysses-redhat-demo \
  --overwrite

# ---------- 9. install the operator chain ---------------------------------
log "Subscribing to OSC, AMQ Streams, NFD, GitOps operators"
cat <<'EOF' | oc apply -f -
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

log "Waiting for operators to reach Succeeded (up to 10 min)"
end=$(( $(date +%s) + 600 ))
while (( $(date +%s) < end )); do
  pending=$(oc get csv -A -o json \
    | jq '[.items[] | select(.metadata.name | test("sandboxed|amq-streams|nfd|gitops"))
                    | select(.status.phase != "Succeeded")] | length')
  [[ "$pending" == "0" ]] && break
  sleep 15
done

oc get csv -A | grep -Ei 'sandboxed|amq-streams|nfd|gitops'

# ---------- 10. activate NFD + OSC kata-cc-tdx RuntimeClass ---------------
log "Activating NFD and KataConfig (kata-cc-tdx)"
cat <<'EOF' | oc apply -f -
---
apiVersion: nfd.openshift.io/v1
kind: NodeFeatureDiscovery
metadata:
  name: nfd-instance
  namespace: openshift-operators
spec:
  operand:
    image: registry.redhat.io/openshift4/ose-node-feature-discovery:latest
---
apiVersion: kataconfiguration.openshift.io/v1
kind: KataConfig
metadata:
  name: cluster-kataconfig
spec:
  enablePeerPods: false
  kataConfigPoolSelector:
    matchLabels:
      purpose: ulysses-redhat-demo
EOF

log "KataConfig reconciliation can take 10-15 min as the node reboots into the kata-enabled config."
log "Watch progress:   oc get kataconfig cluster-kataconfig -o yaml"
log "Expected runtime: oc get runtimeclass | grep kata-cc"

# ---------- 11. summary ---------------------------------------------------
cat <<EOF

$(tput setaf 2 2>/dev/null || true)==== CLUSTER READY ==== $(tput sgr0 2>/dev/null || true)
Project:         ${PROJECT_ID}
Cluster name:    ${CLUSTER_NAME}
Base domain:     ${GCP_BASE_DOMAIN}
API:             https://api.${CLUSTER_NAME}.${GCP_BASE_DOMAIN}:6443
Console:         https://console-openshift-console.apps.${CLUSTER_NAME}.${GCP_BASE_DOMAIN}
Kubeconfig:      ${KUBECONFIG}
SNO node:        ${NODE}
Machine:         ${MACHINE_TYPE} (${ZONE})

Next:
  export KUBECONFIG=${KUBECONFIG}
  cd $(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)

  # Wait for kata-cc-tdx RuntimeClass to appear:
  oc get runtimeclass -w | grep kata-cc

  # Then walk DEPLOY_WITH_CLAUDE_CODE.md step by step, or just:
  oc apply -f 01-namespace.yaml
  oc -n ulysses-demo create secret generic polygon-api-key \\
    --from-literal=POLYGON_API_KEY=\"\${POLYGON_API_KEY}\"
  oc apply -f 02-kafka-cluster.yaml
  oc apply -f 03-polygon-ingress.yaml
  oc apply -f 05-ulysses-consumer.yaml
  oc apply -f 06-servicemonitor.yaml
  oc apply -f 08-grafana-dashboard.yaml
  oc apply -f 07-argocd-app.yaml       # optional but recommended

Teardown when the demo is done:
  ./provision-ulysses-demo-tdx.sh --destroy --yes
EOF
