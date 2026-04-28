# Phase 2 Provision Plan — Azure TDX-attested OpenShift cluster

This document is the plan-of-record for **`ulysses-tdx-demo`**, the Azure
counterpart to the GCP `ulysses-demo` cluster. Where the GCP cluster runs
on a non-confidential `c3-standard-44` host with plain Kata for per-pod
isolation (host-level TDX wasn't first-class in OCP install-config, see
`feedback_ocp_console_dashboard_v14_schema` and `project_peer_pods_subproject`
in auto-memory), the Azure cluster runs on a **TDX-confidential VM** with
host-level Trust Domain protection enabled by IPI itself — no post-install
patches, no per-pod fallback dance. Schema-validated against
`openshift-install 4.21.9` on 2026-04-27.

## Cluster shape

| Property | Value | Notes |
|---|---|---|
| Cluster name | `ulysses-tdx-demo` | Mirrors GCP naming pattern |
| Topology | SNO — 1 master, 0 workers | Same as GCP demo |
| Master SKU | `Standard_DC8es_v6` | 8 vCPU, 32 GiB, Intel Emerald Rapids + TDX |
| Subscription | `de1d31bb-a820-4ca9-a72d-280b0f43d961` (`Azure subscription 1`) | Tenant `kavara.ai` (`b260c4c7-7e87-4ba8-9fd2-9893097705e5`) |
| Region | `westus3` | Only US region with DCesv6 GA + sub access |
| Availability zone | `1` | Single-zone SNO |
| Service principal | `kavara-ocp-installer` (clientId `698eafb0-2852-4f8d-8644-d803da3e9244`) | Roles: Contributor + User Access Administrator on the sub |
| SP creds | `~/.azure/osServicePrincipal.json` (4-key OCP shape) | NEVER commit; gitignored |
| DNS strategy | Azure DNS zone `kavara-azure.local` in `kavara-tdx-dns-rg` | Mirrors `kavara.gcp.local` pattern; `.local` TLD is fake-routable, IPI writes records, /etc/hosts handles browser lookup |
| Pull secret | `$RH_PULL_SECRET` env var (from console.redhat.com) | Same as GCP |
| Cluster CIDR | `10.128.0.0/14` | OCP default |
| Service CIDR | `172.30.0.0/16` | OCP default |
| Machine network | `10.0.0.0/16` | OCP default |
| Network plugin | OVNKubernetes | OCP default |

## Schema validation (the load-bearing finding)

OCP **4.21.9's** install-config exposes Azure ConfidentialVM as a first-class
field on `controlPlane.platform.azure.settings`. Concretely:

```yaml
controlPlane:
  platform:
    azure:
      type: Standard_DC8es_v6
      osDisk:
        diskSizeGB: 256
        diskType: Premium_LRS
        securityProfile:
          securityEncryptionType: VMGuestStateOnly   # or DiskWithVMGuestState
      settings:
        securityType: ConfidentialVM                  # or TrustedLaunch
        confidentialVM:
          uefiSettings:
            secureBoot: Enabled
            virtualizedTrustedPlatformModule: Enabled
```

**This is what the GCP path didn't have.** Per `project_peer_pods_subproject`
auto-memory, GCP CVMs disable nested virt so plain Kata can't run on a
confidential host; the GCP demo therefore uses non-CVM `c3-standard-44`
with `kata` runtime (process isolation, no host-level TDX). The Azure
path doesn't have that compromise — TDX is on at the host, the Kata-CC-TDX
RuntimeClass becomes the right choice for `ulysses-sor` pods.

Field reference (validated by `openshift-install explain`):
- `installconfig.controlPlane.platform.azure.settings` → has `securityType`, `confidentialVM`, `trustedLaunch`
- `installconfig.controlPlane.platform.azure.settings.confidentialVM.uefiSettings` → has `secureBoot`, `virtualizedTrustedPlatformModule` (both `Enabled`/`Disabled`)
- `installconfig.controlPlane.platform.azure.osDisk.securityProfile` → has `diskEncryptionSet`, `securityEncryptionType` (`VMGuestStateOnly` or `DiskWithVMGuestState`)
- `installconfig.controlPlane.platform.azure.osDisk.securityProfile.securityEncryptionType` → constraint: `VMGuestStateOnly` requires `vTPM=Enabled`; `DiskWithVMGuestState` requires both `secureBoot=Enabled` AND `vTPM=Enabled`. We satisfy both, so either value works; we pick `VMGuestStateOnly` (lighter — doesn't require Customer-Managed-Key DiskEncryptionSet).

## Phase 1 cleanup — DONE

| Step | Result |
|---|---|
| Burn `kavara-sno-rg` (stale non-TDX D16s_v5 SNO) | ✅ `ResourceGroupNotFound` (deleted) |
| Create SP `kavara-ocp-installer` + Contributor + UAA roles | ✅ Both roles confirmed via `az role assignment list` |
| Save SP creds to `~/.azure/osServicePrincipal.json` (4-key OCP format, chmod 600) | ✅ 233 bytes, 4 keys, all populated |
| Create DNS zone `kavara-azure.local` in `kavara-tdx-dns-rg` | ✅ Zone created in `westus3` with default NS+SOA records |

## Phase 2 — execution sequence

1. **`./cluster/provision-ulysses-demo-tdx-azure.sh`** — runs preflight
   (tools, SP creds shape, role assignments, DCEV6 quota fit, DNS zone
   sanity), renders `install-config.yaml` from
   `cluster/sno/install-config-template-azure.yaml`, runs
   `openshift-install create cluster`. Verifies the explicit
   "Install complete!" marker before declaring success (lessons from
   the GCP 2026-04-24 incident where openshift-install returned 0
   mid-flight on a CAPI error).

2. **TDX activation verification** — once the cluster is up:
   ```
   oc debug node/$(oc get nodes -o name | head -1) -- \
     chroot /host /bin/bash -c "dmesg | grep -iE 'tdx|cc-trusted-domain' | head -10"
   oc debug node/$(oc get nodes -o name | head -1) -- chroot /host \
     grep tdx /proc/cpuinfo | head -3
   ```
   Expect: `tdx: Guest detected`, `Memory Encryption Features active: Intel TDX`,
   `tdx_guest` in `/proc/cpuinfo` flags. Same evidence shape as the
   `openshift-intel-tdx` capture from 2026-04-22 (see
   `project_pr_faq_evidence_intel_tdx_cell` memory).

3. **Workload deployment** — same 8 numbered YAMLs as GCP, with one
   diff in `05-ulysses-consumer.yaml`:
   ```yaml
   # GCP variant (current source-of-truth):
   runtimeClassName: kata
   # Azure variant (flip back when deploying here):
   runtimeClassName: kata-cc-tdx
   ```
   The `feedback_…` comment block in `05-ulysses-consumer.yaml` already
   documents this Azure-vs-GCP split — uncomment the kata-cc-tdx line
   when applying to `ulysses-tdx-demo`. The OSC operator + KataConfig
   provision both `kata` and `kata-cc-tdx` RuntimeClasses on a TDX-host
   cluster; on this Azure cluster, the latter is the right one.

4. **GitOps wire-up** — separate ArgoCD Application pointing at the
   same `demo-manifests/` repo path, with a different destination
   namespace pattern OR a kustomize overlay distinguishing GCP vs Azure
   variants. **Open question** — see below.

## Open questions / risks

- **kata-cc-tdx availability on DCesv6.** Confirmed in the OSC
  release-notes that Kata-CC-TDX RuntimeClass works on Intel TDX hosts;
  not yet empirically verified on `Standard_DC8es_v6` specifically.
  Worst case: fall back to plain `kata` like the GCP demo.
- **Quota for sustained demo runs.** DCEV6 has 350 vCPU limit in
  westus3 — ample for the SNO + future expansion to multi-node. No
  pre-emptive quota request needed.
- **DNS strategy collision.** `kavara-azure.local` and `kavara.gcp.local`
  both reside in the kavara.ai namespace conceptually; need to make sure
  /etc/hosts entries on a single laptop don't conflict (different
  cluster names — `ulysses-tdx-demo` vs `ulysses-demo` — disambiguate
  the hostnames).
- **Argo Application split.** Two clusters watching the same Git path
  → if a manifest change disrupts one, it disrupts the other. Two
  options: (a) duplicate manifests under `demo-manifests-azure/`
  with kustomize overlay, (b) single manifest set + cluster-specific
  patches via the Application's syncPolicy.ignoreDifferences pattern
  we already use. Lean toward (b) for keynote consistency.
- **Cost.** DC8es_v6 in westus3 list price is ~$0.78/hr ≈ $19/day ≈ $560/mo
  always-on. Acceptable for demo-period; **between-demos mode** is
  cordon + node-shutdown rather than full destroy (preserves cluster
  state so spin-up is minutes, not 30–45). See "Between-demos mode"
  section below.
- **Image registry on Confidential VMs.** OCP's internal image registry
  defaults to a PVC-backed storage. Need to verify the chosen storage
  class on Azure (`managed-premium` is the default) doesn't conflict
  with ConfidentialVM disk policies. Same-class issue as the GCP
  `pd-standard` vs `pd-ssd` we hit on c3 hosts.

## Comparison to GCP path

| Aspect | GCP `ulysses-demo` | Azure `ulysses-tdx-demo` |
|---|---|---|
| Host TDX | NOT enabled (c3 standard VM) | **Enabled** (DC8es_v6 ConfidentialVM) |
| Per-pod isolation | `kata` (process/kernel) | `kata-cc-tdx` (full Trust Domain) |
| Install-config TDX support | None — fell back to Kata | First-class (`settings.confidentialVM`) |
| Storage class trap | `pd-standard` rejected by c3 → `ssd-csi` | TBD — verify `managed-premium` works on DC*es_v6 |
| DNS | `kavara.gcp.local` (Cloud DNS) | `kavara-azure.local` (Azure DNS) |
| Auth | Mint mode + `iam.disableServiceAccountKeyCreation` org-policy lift | Mint mode + SP at `~/.azure/osServicePrincipal.json` |
| Pull secret | `$RH_PULL_SECRET` env | Same |
| Provisioning script | `provision-ulysses-demo-tdx.sh` | `provision-ulysses-demo-tdx-azure.sh` |
| Install-config template | `install-config-template.yaml` | `install-config-template-azure.yaml` |

The Azure path is **architecturally cleaner** for the keynote story:
"deploy a confidential workload on confidential infrastructure with no
runtime-class workarounds." The GCP path becomes the "legacy / baseline
non-TDX-host fallback" comparison point.

## Between-demos mode (cost guard)

Unlike the GCP `ulysses-demo` cluster (kept always-on through 2026-04-28
for Intel Fed Summit), the Azure `ulysses-tdx-demo` cluster doesn't need
24/7 uptime. Default state outside active demo windows is "scaled down,
not destroyed" — preserves cluster + manifests + GitOps wiring; spin-up
is the cost of booting the VM, not re-running 30–45 min of IPI.

**Scale-down (end of demo day):**
```bash
# 1. Cordon the master so nothing schedules during the down window.
oc adm cordon $(oc get nodes -o name | head -1)

# 2. Stop the VM via Azure CLI (deallocate — stops compute billing).
RG=$(az group list --query "[?starts_with(name,'ulysses-tdx-demo-')].name | [0]" -o tsv)
VM=$(az vm list --resource-group "$RG" --query "[?contains(name,'master')].name | [0]" -o tsv)
az vm deallocate --resource-group "$RG" --name "$VM"

# Storage + LB + DNS records continue to bill (~$3-5/day) but compute is $0.
```

**Spin-up (start of next demo):**
```bash
# 1. Start the VM.
az vm start --resource-group "$RG" --name "$VM"

# 2. Wait for kubelet to register (~2-3 min), then uncordon.
until oc get nodes 2>/dev/null | grep -q ' Ready '; do sleep 5; done
oc adm uncordon $(oc get nodes -o name | head -1)
oc get clusteroperators  # confirm all Available=True before demoing
```

**Calendar reminder pattern (preferred over CronJob):** A CronJob can't
deallocate the VM it's running on (chicken-and-egg). Use a personal
calendar reminder + the `az vm deallocate` command above. If you want
automation later, the right shape is an Azure Function with subscription-
scoped Contributor + a schedule trigger — out of scope for the demo.

**When to actually destroy** (`./provision-ulysses-demo-tdx-azure.sh
--destroy --yes`): only when the demo period ends or you need to free
DCEV6 quota for a different cluster shape. Otherwise the cordon+deallocate
pattern is materially cheaper than rebuilding.

## What's NOT in this plan (deferred to Phase 3)

- Multi-zone HA (3 masters across `westus3-1/2/3`)
- Worker nodes (current SNO has the SOR consumer on the master itself)
- Cross-cloud federation (GCP demo + Azure demo as a single visual story
  with both clusters streaming entropy into a shared dashboard)
- AMD SEV-SNP variant (DCasv6 family — would be a third dimension of the
  substrate matrix)

## Halt point

Phase 1 cleanup is done; Phase 2 deliverables (this plan, install-config
template, provisioning script) are drafted. **Awaiting JE review before
running `./provision-ulysses-demo-tdx-azure.sh`** — wall-clock 30–45 min
to live cluster, after which we run TDX activation verification and
proceed to workload deployment.
