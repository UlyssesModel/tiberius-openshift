# demo-manifests

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
Console dashboard. The primary entry point is
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
