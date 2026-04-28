#!/usr/bin/env bash
#
# Provision the Ulysses TDX demo cluster on Azure.
#
#   • Target:     Single-Node OpenShift (SNO) on Standard_DC8es_v6
#                 (Intel Emerald Rapids + TDX) in westus3. Master VM
#                 is a Confidential VM — host-level TDX is on, vTPM
#                 + SecureBoot enabled, VMGuestStateOnly encryption.
#                 Demo workload runs on the same SNO node.
#   • Installer:  OpenShift IPI (installer-provisioned). Azure 4.21.x's
#                 install-config exposes ConfidentialVM type natively
#                 (unlike the GCP path which had to fall back to plain
#                 Kata). Means no post-install MachineSet patch.
#   • Auth:       Mint mode via SP at ~/.azure/osServicePrincipal.json
#                 (created out-of-band — see PROVISION_PLAN.md). SP needs
#                 Contributor + User Access Administrator on the sub.
#   • DNS:        Azure DNS zone kavara-azure.local in kavara-tdx-dns-rg.
#                 .local TLD, fake-routable, mirrors the GCP pattern.
#   • Output:     KUBECONFIG at ${INSTALL_DIR}/auth/kubeconfig.
#
# Lessons-learned guards (mirrored from the GCP script after 2026-04-25):
#   * Quota fit check at machine-type granularity (need ≥8 vCPU in
#     Standard DCEV6 Family).
#   * Stale install-state guard refuses to overwrite without --reset.
#   * SP creds + role assignment preflight (don't let Manual-mode bite
#     us like the GCP first attempt did).
#   * Post-install grep for "Install complete!" — openshift-install can
#     return 0 mid-flight on CAPI errors without ever reaching bootstrap-
#     complete; verify the explicit milestone.
#
# Usage:
#   export RH_PULL_SECRET='{"auths":...}'                  # console.redhat.com
#   ./provision-ulysses-demo-tdx-azure.sh [--yes] [--destroy] [--reset]
#
# Capturing output:
#   ./provision-ulysses-demo-tdx-azure.sh --yes >/tmp/install.log 2>&1   # GOOD
#   ./provision-ulysses-demo-tdx-azure.sh --yes 2>&1 | tee /tmp/install.log; \
#     exit ${PIPESTATUS[0]}                                              # GOOD
#   ./provision-ulysses-demo-tdx-azure.sh --yes | tee /tmp/install.log   # BAD
#                                                # ↑ tee returns 0 even
#                                                # if installer errors;
#                                                # set -o pipefail INSIDE
#                                                # this script doesn't
#                                                # reach the outer pipe.
#
# ---------------------------------------------------------------------------

# pipefail is part of the -euo pipefail invocation below; controls pipes
# WITHIN this script. Outer pipes (caller's `| tee` etc.) are not affected
# — see Capturing output guidance above.
set -euo pipefail

# Defensive: warn if the script is being piped on the OUTER side. tty test
# detects stdout-not-attached-to-terminal; that's the OUTER `| tee` /
# `| less` shape. Doesn't fail (some legitimate uses, e.g., CI redirect to
# file via `>`), just nudges the user.
if [[ ! -t 1 ]]; then
  printf '\033[1;33m[%s]\033[0m %s\n' "$(date +%H:%M:%S)" \
    "stdout is not a TTY — if you're piping to tee, exit code may be masked. Use redirect (>file 2>&1) or PIPESTATUS." >&2
fi

# ---------- config knobs --------------------------------------------------
SUBSCRIPTION_ID="${SUBSCRIPTION_ID:-de1d31bb-a820-4ca9-a72d-280b0f43d961}"
TENANT_ID="${TENANT_ID:-b260c4c7-7e87-4ba8-9fd2-9893097705e5}"
REGION="${REGION:-westus3}"
# DCesv6 SKUs aren't uniformly zoned — Azure pins TDX silicon to specific zones
# per region. As of 2026-04-27 westus3 has DC8es_v6 in zone 3 ONLY. Picking
# the wrong zone fails ~60 min into install with OverconstrainedZonalAllocation
# Request and a misleading downstream DNS error. Preflight below verifies zone
# fit at SKU granularity before letting the installer near anything. See
# feedback_azure_dcesv6_zone_pinning auto-memory for prior incident.
ZONE="${ZONE:-3}"
CLUSTER_NAME="${CLUSTER_NAME:-ulysses-tdx-demo}"
# DC8es_v6: 8 vCPU, 32 GiB, Intel TDX-capable. The minimum DCesv6 size
# usable for an SNO master that fits Kafka + monitoring + some headroom.
# Bump to DC16es_v6 / DC32es_v6 if the SOR consumer needs more vCPU.
MACHINE_TYPE="${MACHINE_TYPE:-Standard_DC8es_v6}"
BOOT_DISK_SIZE_GB="${BOOT_DISK_SIZE_GB:-256}"
OCP_VERSION="${OCP_VERSION:-4.21.9}"
INSTALL_DIR="${INSTALL_DIR:-$(pwd)/ulysses-tdx-demo-install}"
SSH_KEY_PATH="${SSH_KEY_PATH:-${HOME}/.ssh/id_rsa_sno.pub}"
SP_CREDS_FILE="${SP_CREDS_FILE:-${HOME}/.azure/osServicePrincipal.json}"
DNS_RG="${DNS_RG:-kavara-tdx-dns-rg}"
DNS_ZONE="${DNS_ZONE:-kavara-azure.local}"
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
  log "Destroying cluster via openshift-install"
  confirm "Proceed?" || die "aborted"
  openshift-install destroy cluster --dir="${INSTALL_DIR}" --log-level=info
  log "Done. Verify no orphans:"
  az group list --query "[?contains(name, '${CLUSTER_NAME}')].{name:name, location:location}" -o table
  exit 0
fi

# ---------- 1. preflight ---------------------------------------------------
log "Preflight: tools + auth + sub"

for bin in az openshift-install oc jq curl python3; do
  command -v "$bin" >/dev/null 2>&1 || die "missing tool: $bin"
done

[[ -r "${SSH_KEY_PATH}" ]] || die "ssh pubkey not readable: ${SSH_KEY_PATH}"

# Mint-mode credentials — IPI on Azure looks for the SP at this exact path.
# Verify presence + 4-key shape.
[[ -r "${SP_CREDS_FILE}" ]] \
  || die "SP creds file not readable: ${SP_CREDS_FILE}.
  Create via:
    az ad sp create-for-rbac --name kavara-ocp-installer \\
      --role Contributor \\
      --scopes /subscriptions/${SUBSCRIPTION_ID}
    az role assignment create --assignee <appId> \\
      --role 'User Access Administrator' \\
      --scope /subscriptions/${SUBSCRIPTION_ID}
  Save in ~/.azure/osServicePrincipal.json with shape:
    {subscriptionId, clientId, clientSecret, tenantId}"

# Sanity: 4 keys present + each non-empty
SP_KEYS=$(jq -r 'keys | length' "${SP_CREDS_FILE}")
[[ "${SP_KEYS}" == "4" ]] || die "SP creds file has ${SP_KEYS} keys, expected 4"
for k in subscriptionId clientId clientSecret tenantId; do
  v=$(jq -r ".${k}" "${SP_CREDS_FILE}")
  [[ -n "$v" && "$v" != "null" ]] || die "SP creds field '${k}' is empty"
done
SP_APP_ID=$(jq -r .clientId "${SP_CREDS_FILE}")
log "  SP creds: ${SP_CREDS_FILE} (clientId: ${SP_APP_ID})"

# Verify SP has BOTH required role assignments. Manual mode in OCP-on-Azure
# is fragile (similar trap to the GCP Manual-mode bug); Mint mode needs
# Contributor (resource creation) + User Access Administrator (RBAC writes
# for cluster-internal SPs).
HAS_CONTRIB=$(az role assignment list --assignee "${SP_APP_ID}" --scope "/subscriptions/${SUBSCRIPTION_ID}" --query "[?roleDefinitionName=='Contributor'] | length(@)" -o tsv 2>/dev/null || echo 0)
HAS_UAA=$(az role assignment list --assignee "${SP_APP_ID}" --scope "/subscriptions/${SUBSCRIPTION_ID}" --query "[?roleDefinitionName=='User Access Administrator'] | length(@)" -o tsv 2>/dev/null || echo 0)
[[ "$HAS_CONTRIB" == "1" ]] || die "SP missing Contributor role on /subscriptions/${SUBSCRIPTION_ID}"
[[ "$HAS_UAA" == "1" ]]     || die "SP missing User Access Administrator role on /subscriptions/${SUBSCRIPTION_ID}"
log "  SP roles confirmed: Contributor + User Access Administrator"

# Sub state
SUB_STATE=$(az account show --subscription "${SUBSCRIPTION_ID}" --query state -o tsv 2>/dev/null || echo "Unknown")
[[ "${SUB_STATE}" == "Enabled" ]] || die "subscription state is ${SUB_STATE}, expected Enabled"

log "Installer version:"
openshift-install version

# ---------- 2a. zone availability for the SKU (TDX hardware placement) ----
# Azure DCesv6 SKUs aren't uniformly distributed across zones — TDX silicon
# is racked in specific zones per region. Skipping this gate burns ~60 min
# before failing with OverconstrainedZonalAllocationRequest (misleading DNS
# error downstream).
log "Checking zone availability for ${MACHINE_TYPE} in ${REGION}"
SKU_ZONES=$(az vm list-skus --location "${REGION}" --size "${MACHINE_TYPE}" \
  --query "[].locationInfo[0].zones[]" -o tsv 2>/dev/null | tr '\n' ',' | sed 's/,$//')
[[ -n "${SKU_ZONES}" ]] || die "${MACHINE_TYPE} has NO zone availability in ${REGION} right now.
  Try a different region or different SKU. Query:
    az vm list-skus --location <region> --size ${MACHINE_TYPE} --query '[].locationInfo[0].zones'"
log "  ${MACHINE_TYPE} available zones in ${REGION}: [${SKU_ZONES}]"
case ",${SKU_ZONES}," in
  *",${ZONE},"*) log "  ZONE=${ZONE} matches available — proceeding." ;;
  *) die "${MACHINE_TYPE} not available in ZONE=${ZONE} (available: [${SKU_ZONES}]).
  Re-run with ZONE=<one of those>, e.g.  ZONE=$(echo "${SKU_ZONES}" | cut -d, -f1) $0 --yes" ;;
esac

# ---------- 2b. quota fit check (DC8es_v6 needs ≥8 vCPU in DCEV6 family) --
log "Checking DCEV6 quota in ${REGION} (need ≥8 vCPU for 1 x ${MACHINE_TYPE})"
DCEV6_LIMIT=$(az vm list-usage --location "${REGION}" --query "[?name.value=='standardDCEV6Family'].limit | [0]" -o tsv 2>/dev/null || echo 0)
DCEV6_USED=$(az vm list-usage --location "${REGION}" --query "[?name.value=='standardDCEV6Family'].currentValue | [0]" -o tsv 2>/dev/null || echo 0)
DCEV6_AVAIL=$(( DCEV6_LIMIT - DCEV6_USED ))
log "  DCEV6: used ${DCEV6_USED} / limit ${DCEV6_LIMIT} -> available ${DCEV6_AVAIL}"
NEED_PER_VM=8  # DC8es_v6 = 8 vCPU
if (( NEED_PER_VM > DCEV6_AVAIL )); then
  die "DCEV6 quota fit check failed: need ${NEED_PER_VM} for ${MACHINE_TYPE}, have ${DCEV6_AVAIL}.
  Either reduce MACHINE_TYPE (DC2es_v6 = 2 vCPU is the smallest), stop other
  DCEV6 instances, or request quota increase via Azure portal."
fi

# ---------- 2c. /etc/hosts preflight (warn-only) --------------------------
# `.local` baseDomain isn't delegatable to public DNS — the laptop running
# the installer can't resolve `api.<cluster>.<base>` via Azure DNS. Once
# bootstrap-complete fires, the installer's API-wait gate hangs at DNS
# NXDOMAIN unless /etc/hosts has an explicit A record pointing at the API
# LB public IP. We can't auto-add it (sudo + LB IP not available until
# Azure CAPI provisions the LB mid-install), but surface the WARN early
# so the operator knows to watch for it. See feedback_etc_hosts_no_wildcard
# auto-memory for the wildcard trap (* doesn't work in /etc/hosts).
API_HOST="api.${CLUSTER_NAME}.${DNS_ZONE}"
if grep -q -- "${API_HOST}" /etc/hosts 2>/dev/null; then
  log "  /etc/hosts has an entry for ${API_HOST} (good)."
else
  warn "/etc/hosts has NO entry for ${API_HOST}."
  warn "  After Azure CAPI provisions the API LB (~10-15 min into install),"
  warn "  you'll need to patch /etc/hosts as the installer reaches its"
  warn "  'Waiting up to 20m0s ... for the Kubernetes API at https://${API_HOST}:6443'"
  warn "  step. Find the LB IP and append the entry:"
  warn "    APPS_RG=\$(az group list --query \"[?starts_with(name,'${CLUSTER_NAME}-')].name | [0]\" -o tsv)"
  warn "    LB_IP=\$(az network public-ip list -g \$APPS_RG --query \"[?contains(name,'pip-v4')].ipAddress | [0]\" -o tsv)"
  warn "    sudo sh -c \"echo '\$LB_IP ${API_HOST}' >> /etc/hosts\""
  warn "  AND for browser access (Grafana / OCP Console), patch the apps"
  warn "  hostnames explicitly (NO wildcards — see feedback_etc_hosts_no_wildcard):"
  warn "    APPS_IP=\$(oc -n openshift-ingress get svc router-default -o jsonpath='{.status.loadBalancer.ingress[0].ip}')"
  warn "    for h in console-openshift-console oauth-openshift \\"
  warn "             ulysses-grafana-route-grafana-system \\"
  warn "             prometheus-k8s-openshift-monitoring \\"
  warn "             thanos-querier-openshift-monitoring; do"
  warn "      echo \"\$APPS_IP \$h.apps.${CLUSTER_NAME}.${DNS_ZONE}\""
  warn "    done | sudo tee -a /etc/hosts"
fi

# ---------- 3. stale install-state guard ----------------------------------
if [[ -d "${INSTALL_DIR}/.openshift_install_state.json" ]] || [[ -f "${INSTALL_DIR}/auth/kubeconfig" ]]; then
  if [[ "${DO_RESET}" == "1" ]]; then
    log "RESET: clearing prior install state at ${INSTALL_DIR}"
    rm -rf "${INSTALL_DIR}"
  else
    die "Prior install state exists at ${INSTALL_DIR}.
  Either:
    1) Run with --destroy to tear down the old cluster cleanly, OR
    2) Run with --reset to wipe local state (use ONLY after destroy)."
  fi
fi

# ---------- 4. DNS zone sanity --------------------------------------------
log "Confirming DNS zone exists: ${DNS_ZONE} in ${DNS_RG}"
az network dns zone show --resource-group "${DNS_RG}" --name "${DNS_ZONE}" --query 'name' -o tsv >/dev/null \
  || die "DNS zone ${DNS_ZONE} not found in RG ${DNS_RG}.
  Create via:
    az group create --name ${DNS_RG} --location ${REGION}
    az network dns zone create --resource-group ${DNS_RG} --name ${DNS_ZONE}"

# ---------- 5. render install-config.yaml ---------------------------------
log "Rendering install-config.yaml into ${INSTALL_DIR}"
mkdir -p "${INSTALL_DIR}"
SSH_PUBKEY="$(cat "${SSH_KEY_PATH}")"

TEMPLATE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
[[ -r "${TEMPLATE_DIR}/install-config-template-azure.yaml" ]] \
  || die "missing template: ${TEMPLATE_DIR}/install-config-template-azure.yaml"

python3 - <<PY > "${INSTALL_DIR}/install-config.yaml"
import os, json
t = open("${TEMPLATE_DIR}/install-config-template-azure.yaml").read()
t = t.replace("__CLUSTER_NAME__",  "${CLUSTER_NAME}")
t = t.replace("__BASE_DOMAIN__",   "${DNS_ZONE}")
t = t.replace("__REGION__",        "${REGION}")
t = t.replace("__ZONE__",          "${ZONE}")
t = t.replace("__MACHINE_TYPE__",  "${MACHINE_TYPE}")
t = t.replace("__DISK_SIZE__",     "${BOOT_DISK_SIZE_GB}")
t = t.replace("__DNS_RG__",        "${DNS_RG}")
t = t.replace("__SSH_PUBKEY__",    """${SSH_PUBKEY}""")
t = t.replace("__PULL_SECRET__",   json.dumps(json.loads(os.environ["RH_PULL_SECRET"])))
print(t)
PY

cp "${INSTALL_DIR}/install-config.yaml" "${INSTALL_DIR}/install-config.yaml.bak"
log "  written: ${INSTALL_DIR}/install-config.yaml (backup alongside)"

# ---------- 6. run the installer ------------------------------------------
if [[ -f "${INSTALL_DIR}/auth/kubeconfig" ]] && \
   grep -q "Install complete!" "${INSTALL_DIR}/.openshift_install.log" 2>/dev/null; then
  log "Cluster appears already installed. Skipping create."
else
  log "Running openshift-install create cluster (expected wall-clock 30-45 min)"
  confirm "Proceed?" || die "aborted"
  openshift-install create cluster --dir="${INSTALL_DIR}" --log-level=info
fi

# Don't trust openshift-install's exit code alone — verify the explicit
# bootstrap-complete + install-complete markers (lessons from 2026-04-24
# GCP incident).
if ! grep -q "Install complete!" "${INSTALL_DIR}/.openshift_install.log" 2>/dev/null; then
  warn "openshift-install returned 0 but no 'Install complete!' marker."
  warn "Last 20 lines:"
  tail -20 "${INSTALL_DIR}/.openshift_install.log" >&2
  die "Refusing to declare success without bootstrap-complete signal."
fi

# ---------- 7. summary ----------------------------------------------------
export KUBECONFIG="${INSTALL_DIR}/auth/kubeconfig"
log "KUBECONFIG: ${KUBECONFIG}"
oc whoami
oc get nodes -o wide
oc get clusterversion
log "ClusterVersion sanity (no spec.overrides expected):"
oc get clusterversion version -o jsonpath='{.spec.overrides}' && echo

cat <<EOF

$(tput setaf 2 2>/dev/null || true)==== TDX CLUSTER READY ==== $(tput sgr0 2>/dev/null || true)
Cluster name:    ${CLUSTER_NAME}
Subscription:    ${SUBSCRIPTION_ID}
Region/zone:     ${REGION} / ${ZONE}
Machine SKU:     ${MACHINE_TYPE} (Intel Emerald Rapids + TDX, ConfidentialVM)
Base domain:     ${DNS_ZONE}
API:             https://api.${CLUSTER_NAME}.${DNS_ZONE}:6443
Console:         https://console-openshift-console.apps.${CLUSTER_NAME}.${DNS_ZONE}
Kubeconfig:      ${KUBECONFIG}
Kubeadmin pwd:   ${INSTALL_DIR}/auth/kubeadmin-password

Next (verification — JE halt point, complete before porting demo-manifests):
  export KUBECONFIG=${KUBECONFIG}

  # (1) TDX active at the host level: dmesg init lines + cpuid flag.
  oc debug node/\$(oc get nodes -o name | head -1) -- chroot /host /bin/bash -c \\
    "dmesg | grep -iE 'tdx|cc-trusted-domain|memory encryption' | head -10"
  oc debug node/\$(oc get nodes -o name | head -1) -- chroot /host \\
    grep tdx /proc/cpuinfo | head -3
  # Expect: 'Memory Encryption Features active: Intel TDX' in dmesg AND
  # 'tdx_guest' in /proc/cpuinfo flags.

  # (2) kata-cc-tdx RuntimeClass dual-check (AFTER OSC operator + KataConfig
  # apply — that's part of the manifest deploy, not this script). The
  # load-bearing check JE called out:
  #   oc get runtimeclass | grep kata-cc-tdx
  #   oc debug node/\$(oc get nodes -o name | head -1) -- chroot /host \\
  #     bash -c 'dmesg | grep -i tdx'
  # Decision branch:
  #   - kata-cc-tdx REGISTERED + dmesg shows TDX init  -> use kata-cc-tdx
  #     in 05-ulysses-consumer.yaml.
  #   - kata-cc-tdx ABSENT or dmesg empty for TDX -> fall back to plain
  #     kata (mirroring the GCP demo) and document the gap in
  #     PROVISION_PLAN.md "Open questions" section. Both are valid
  #     outcomes for the demo.

  # (3) Then deploy the demo workload — same 8 numbered YAMLs as GCP:
  cd ../        # back to demo-manifests/
  oc apply -f 01-namespace.yaml
  # ... walk DEPLOY_WITH_CLAUDE_CODE.md, flipping ulysses-consumer.yaml's
  # runtimeClassName per the (2) decision branch above.

Teardown:
  ./provision-ulysses-demo-tdx-azure.sh --destroy --yes
EOF
