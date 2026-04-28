# demo-manifests

> [!WARNING]
> **First-time clone note:** If you're pulling this for the first time, the Polygon API key in early commit history was rotated on **2026-04-26**; use your own key. Manifests reference the key via Kubernetes `Secret/polygon-api-key` (see `01-namespace.yaml` + the runbook for how to populate it).

This directory is the **working evolution** of the [tiberius-openshift](../)
reference stub that anchored the April 2026 architectural conversation
with Red Hat. Where tiberius captured the *shape* of the substrate matrix
and the SOR pipeline at the manifest level — the architecture-brief
artifact for that early Red Hat round — `demo-manifests/` is the version
that **actually runs end-to-end**: real Polygon market data → AMQ Streams
(Kafka, KRaft mode, mTLS-authenticated) → Ulysses SOR consumer (Kata
Containers for kernel-level isolation, with `kata-cc-tdx` flagged for the
Azure DCesv6 variant) → entropy stream on `ulysses.entropy.equities` →
OCP Console-rendered Prometheus dashboard. The eight numbered YAMLs
(`01-namespace.yaml` … `08-grafana-dashboard.yaml`) define the workload,
`04-polygon-ingress.py` is the standalone Polygon producer source,
`image/` is the SOR demo image build (UBI9 Python 3.11 base), and
`cluster/` holds the OCP IPI provisioning recipe (Mint-mode install with
the `iam.disableServiceAccountKeyCreation` org-policy precondition
documented inline) plus the SNO install-config template under
`cluster/sno/`.

The full pipeline was deployed live on a GCP Single-Node OpenShift 4.21
cluster (`ulysses-demo`, infraID `2g6tw`, machine type `c3-standard-44`)
on **2026-04-25** and verified end-to-end that evening: **~1.5M trades
consumed** off the `market.equities.trades` topic, **0.836 entropy
messages/sec emitted** at steady state on a 12-second emission cadence,
**~1 ms p95 window-compute latency** in the SOR's per-window entropy
calculation, all 14 keynote-relevant Grafana panels populated in the OCP
Console dashboard. A second cluster (`ulysses-tdx-demo`, infraID
`k8brw`, machine type `Standard_DC16es_v6` in Azure `westus3` zone 3)
was provisioned on **2026-04-27** and verified on **2026-04-28** as the
**TDX-attested counterpart** — same pipeline, host-level Intel TDX
Trust Domain via Azure ConfidentialVM (`Memory Encryption Features
active: Intel TDX` confirmed in `dmesg` + CPUID brand-string
`InteTD X   l` interleaving visible from inside the SOR pod). The two
clusters tell complementary stories: GCP demonstrates per-pod Kata
isolation on a non-confidential host; Azure demonstrates host-level
TDX with native runc (TDX guests block nested virt, so per-pod Kata
isn't a viable path on DCesv6 — see Gotcha #12 below). The Azure
provisioning recipe lives under `cluster/azure/`. The primary entry point is
[`DEPLOY_WITH_CLAUDE_CODE.md`](DEPLOY_WITH_CLAUDE_CODE.md), a step-by-step
runbook designed to be driven via Claude Code as a natural-language
operator: each step is a command Claude proposes, runs, interprets, and
iterates on, with a human in the loop for state-changing actions and
credential prompts. The `WARNING:` and `DEMO SHORTCUT:` comments
scattered through the manifests capture the gotchas hit and fixed during
the live build — Strimzi listener mTLS auth, KafkaUser clients-CA vs
cluster-CA split, `enable.idempotence` cluster ACL, SNO CPU sizing, OCP
Console datasource compatibility — so the next person rebuilding from
this baseline doesn't re-pay the same debugging tax. The Confluence
architecture-conversation thread with Red Hat and the six open
architecture questions still being negotiated (peer-pods on GCP
confidential VMs, AMD SEV-SNP cell parity, Azure DCesv6 GA schedule,
GPU-on-Kata story, attestation handoff, multi-node prod topology) live
in the project workspace, not in this repo.

## Observability

Two rendering paths for the same v40 Grafana dashboard JSON
(`grafana/ulysses-demo-dashboard.json`); the Grafana Operator path is
the active one.

- **Active — Grafana Operator** (`09-grafana-operator.yaml`).
  Community Grafana Operator (channel `v5`) deploys a Grafana 12.x
  instance in `grafana-system`, mounts a 5 Gi PVC at
  `/var/lib/grafana` for persistence, and a bootstrap `Job`
  idempotently pushes the Prometheus datasource via Grafana's admin
  API (working around an Operator v5.22.2 `valuesFrom` substitution
  bug — see `feedback_ocp_thanos_querier_grafana_auth.md` in
  auto-memory). Datasource queries OCP's Thanos-Querier on port 9091,
  which federates platform Prometheus + UWM. Dashboard renders all 18
  panels natively. Browser at the Route hostname (`oc -n grafana-system
  get route ulysses-grafana-route`) — needs the apps wildcard IP in
  /etc/hosts.

- **Deferred — OCP Console** (`08-grafana-dashboard.yaml`). OCP
  Console's built-in dashboard renderer is locked to **legacy Grafana
  schema v14** (rows-based, ~2017 vintage). Our dashboard is
  **schema v40** (panels-based with `gridPos`), so OCP Console lists
  the dashboard but renders the panel area blank. A v14 rewrite is
  the only way to use OCP-native rendering — ~1–2 hours of grunt
  work; lose modern panel types (smooth timeseries, exemplar-aware
  heatmaps) in the conversion. Deferred indefinitely; the Grafana
  Operator path covers the keynote story.

### Known drift (Argo OutOfSync count: 8)

ArgoCD is wired as observer (auto-sync off). The following resources show OutOfSync against the repo and are accepted as the demo state. Cluster runs identically regardless of sync status.

| Resource | Reason | Resolution path (v2 / production) |
|---|---|---|
| `Application/ulysses-demo`, `ArgoCD/openshift-gitops` | Self-tracking paradox — committing changes to `07-argocd-app.yaml` updates the Application CR which Argo tracks. Argo status fields drift continuously. | Exclude self-references from Argo's resource scope, or use ApplicationSet pattern. |
| `Job/grafana-datasource-bootstrap`, `GrafanaDatasource/prometheus-thanos` | Removed from Git in commit `1707a11`; Argo tracker has phantom memory. Job ran successfully + TTL'd; GrafanaDatasource bypassed via API push due to Grafana Operator v5.22.2 secureJsonData substitution bug. | Tracker purges over time. Already removed from manifests. |
| `Deployment/polygon-ingress`, `Kafka/kafka-cluster`, `KafkaUser/polygon-ingress` | Real cluster-side fields (admission-webhook defaults, operator-injected) not yet in Git YAML. | Diff individual resources, write back to YAML, or add resource-specific `ignoreDifferences`. |
| `Deployment/ulysses-sor` | Rolling-update artifact from yesterday's iterative debugging. `dcls7` pod is the actual healthy SOR (running 20h+, producing entropy live). New ReplicaSet pods can't fit on SNO at 16-CPU sizing. | `oc rollout restart` after current pod state is stable, or scale-up the SNO. |

All three live measurements (1.5M trades, 0.873 entropies/s, 1ms p95 window compute) reflect this exact cluster state. The OutOfSync count is GitOps hygiene work, not a functional issue.

## Known operational gotchas

The full list lives in the project's Confluence "painful gotchas"
table; the entries below are the ones added during the Azure
`ulysses-tdx-demo` build (2026-04-27/28). Each links to an
auto-memory file with the failing-symptom, root cause, and applied
fix.

| # | Symptom | Root cause | Applied fix |
|---|---|---|---|
| 10 | OCP IPI install on Azure runs ~60 min, then both bootstrap + master VMs report `ProvisioningState/failed/OverconstrainedZonalAllocationRequest`. The downstream installer error is a misleading DNS lookup failure for the cluster API. | DCesv6 (Intel TDX) SKUs aren't uniformly distributed across Azure availability zones — TDX silicon is racked in specific zones per region. As of 2026-04-27, `Standard_DC8es_v6` in `westus3` is available in **zone 3 only**. Pinning the wrong zone burns the full installer wall-clock before failing. | Preflight zone fit at SKU granularity in `cluster/azure/provision-ulysses-demo-tdx-azure.sh`: `az vm list-skus --location $REGION --size $MACHINE_TYPE --query "[].locationInfo[0].zones[]"` — die before installer if `$ZONE` isn't in the returned set. |
| 11 | TDX activation verification scripts using `grep tdx /proc/cpuinfo` return no matches on the master node, suggesting TDX isn't active. | RHCOS 9.x ships kernel 5.14, which doesn't backport the textual `tdx_guest` cpufeature flag (added upstream in 5.19). The `/sys/firmware/coco/` CoCo-subsystem path is also absent. **TDX is genuinely active** — kernel detection just isn't surfaced textually on this kernel. | Verify via the load-bearing signals instead: `dmesg` shows `Memory Encryption Features active: Intel TDX` + `systemd[1]: Detected confidential virtualization tdx`; `/proc/cpuinfo` `model name` reads as `InteTD X   l` (Azure's TDX brand-string injection); NFD applies `feature.node.kubernetes.io/cpu-cpuid.TDX_GUEST=true` and `cpu-security.tdx.protected=true` (NFD does its own cpuid query independent of `/proc/cpuinfo`). |
| 12 | On Azure DCesv6, plain `kata` runtime fails every pod with `FailedCreatePodSandBox: rpc error: code = DeadlineExceeded`, even with abundant CPU/memory headroom. | TDX guests do not expose VMX/KVM (`grep -c vmx /proc/cpuinfo` → 0; `/dev/kvm` absent) — nested virt is blocked by design as part of the Trust Domain attack-surface reduction. Kata needs KVM to spin its inner VM; without it, sandbox creation deadlines out. | **Substrate dictates the per-pod runtime.** On Azure DCesv6 omit `runtimeClassName` (use `runc`) — the host VM is itself the Trust Domain, so the confidential boundary is at the VM layer, not the pod layer. On GCP `c3-standard-44` (non-CVM, nested virt available, but no host-TDX) keep `runtimeClassName: kata` for per-pod isolation. The two patterns are complementary, not competing — see comment block in `05-ulysses-consumer.yaml`. For per-pod confidential containers on TDX hosts, the path is the Confidential Containers Operator (CCO, dev-preview as of 2026-04-27) which uses peer-pods to avoid nested virt; tracked separately. |

## Phase 2 polish (deferred from 2026-04-26)

Three dashboard panels are intentionally empty pending follow-up wiring;
the 14 entropy + window-compute panels (the keynote-relevant ones)
populate cleanly without these:

- **Panel 3 — Ingress errors.** Counter `polygon_errors_total` is registered
  in the producer but never increments under healthy operation. Will
  populate the moment any ingress error fires; harmless empty otherwise.
- **Panel 5 — Consumer group lag.** Requires `kafka_consumergroup_lag`,
  which is exposed by Strimzi's `kafka-exporter` sidecar. Enable via
  `Kafka.spec.kafkaExporter: {}` in `02-kafka-cluster.yaml` if lag
  visualization is needed for keynote v2 or post-demo perf work.
- **Panel 8 — SOR replicas ready.** Reads `kube_deployment_status_replicas_ready`
  from kube-state-metrics, which lives in the platform `cluster-monitoring`
  Prometheus stack — UWM doesn't scrape it. Two options if needed: enable
  kube-state-metrics within UWM, or rewrite the panel to use a UWM-native
  equivalent (e.g., `count(up{job="ulysses-sor-metrics"})`).
