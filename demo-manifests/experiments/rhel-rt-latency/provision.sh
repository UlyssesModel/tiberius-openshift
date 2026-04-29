#!/usr/bin/env bash
# Provisions ulysses-rt-bench: a CentOS Stream 9 VM on GCP with kernel-rt
# (PREEMPT_RT) and the workload runtime needed to measure window-compute
# latency for the Ulysses SOR entropy pipeline.
#
# Why CentOS Stream 9 + kernel-rt instead of RHEL 9 + kernel-rt?
#   The architectural claim ("Kavara latency under PREEMPT_RT kernel") is
#   substrate-agnostic. Cloud Access does not include the Red Hat Real Time
#   add-on subscription, and we don't want to block this experiment on a
#   subscription request. CentOS Stream 9's `rt` SIG ships an upstream-
#   tracking kernel-rt with the same PREEMPT_RT semantics. Migrate to
#   RHEL-RT once entitlement lands and the procurement framing requires
#   "Red Hat-supported" specifically.
#
# Usage
#   ./provision.sh                  # idempotent — re-run is safe
#   ./provision.sh teardown         # delete the VM
#
# Dependencies on the operator host
#   gcloud (authenticated as a principal with Compute Admin in the project)
#   oc (authenticated against the ulysses-demo cluster, kubeconfig present)
set -euo pipefail

PROJECT=${PROJECT:-office-of-cto-491318}
ZONE=${ZONE:-us-central1-a}
VM=${VM:-ulysses-rt-bench}
MACHINE=${MACHINE:-c3-standard-8}            # SPR, 8 vCPU, AMX-capable
IMAGE_FAMILY=${IMAGE_FAMILY:-centos-stream-9}
IMAGE_PROJECT=${IMAGE_PROJECT:-centos-cloud}
NAMESPACE=${NAMESPACE:-ulysses-demo}
KAFKA_USER=${KAFKA_USER:-rt-bench}
KAFKA_CLUSTER_CA_SECRET=${KAFKA_CLUSTER_CA_SECRET:-kafka-cluster-cluster-ca-cert}
KAFKA_BOOTSTRAP=${KAFKA_BOOTSTRAP:-kafka-cluster-kafka-bootstrap-ulysses-demo.apps.ulysses-demo.kavara.gcp.local:443}
APPS_LB_IP=${APPS_LB_IP:-34.60.235.190}
SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &> /dev/null && pwd)

teardown() {
  gcloud compute instances delete "$VM" --zone="$ZONE" --project="$PROJECT" --quiet || true
  oc delete kafkauser "$KAFKA_USER" -n "$NAMESPACE" --ignore-not-found
  exit 0
}

[[ "${1:-}" == "teardown" ]] && teardown

# 1) Dedicated Kafka user with read-only ACL on market.equities.* and an
#    isolated consumer group so we don't share partitions with the live SOR pod.
oc apply -n "$NAMESPACE" -f - <<EOF
apiVersion: kafka.strimzi.io/v1beta2
kind: KafkaUser
metadata:
  name: $KAFKA_USER
  namespace: $NAMESPACE
  labels:
    strimzi.io/cluster: kafka-cluster
spec:
  authentication:
    type: tls
  authorization:
    type: simple
    acls:
      - operations: [Read, Describe]
        resource: { type: topic, name: market.equities., patternType: prefix }
      - operations: [Read]
        resource: { type: group, name: rt-bench-eqty }
EOF

# 2) VM
if ! gcloud compute instances describe "$VM" --zone="$ZONE" --project="$PROJECT" >/dev/null 2>&1; then
  gcloud compute instances create "$VM" \
    --project="$PROJECT" --zone="$ZONE" --machine-type="$MACHINE" \
    --image-family="$IMAGE_FAMILY" --image-project="$IMAGE_PROJECT" \
    --boot-disk-size=50GB --boot-disk-type=pd-balanced \
    --network=default --subnet=default --tags=ulysses-rt-bench
fi

ssh_run() {
  gcloud compute ssh "$VM" --zone="$ZONE" --project="$PROJECT" --quiet --command="$*"
}

# 3) kernel-rt + tuned-realtime + kernel boot args. The CentOS Stream 9 `rt`
#    repo is pre-defined but disabled by default; it ships kernel-rt 5.14.0+rt
#    with PREEMPT_RT, plus tuned-profiles-realtime which auto-derives
#    isolcpus/nohz_full/rcu_nocbs from /etc/tuned/realtime-variables.conf.
ssh_run '
set -ex
sudo dnf -y install dnf-plugins-core
sudo dnf config-manager --set-enabled crb rt
sudo dnf -y install kernel-rt kernel-rt-core tuned-profiles-realtime numactl util-linux-core python3.11 python3.11-pip
echo "isolated_cores=2-7" | sudo tee /etc/tuned/realtime-variables.conf
sudo systemctl enable --now tuned
sudo tuned-adm profile realtime
sudo grubby --update-kernel=ALL --args="intel_pstate=disable processor.max_cstate=1 idle=poll skew_tick=1 audit=0 nosoftlockup mce=off"
RT_KERN=$(ls /boot/vmlinuz-*+rt | head -1)
sudo grubby --set-default="$RT_KERN"
python3.11 -m pip install --user --quiet numpy confluent-kafka
'

ssh_run 'sudo systemctl reboot' || true   # SSH drops on reboot; expected.

# 4) Poll until PREEMPT_RT is in /proc/version (uname does NOT include it on
#    el9 — kernel reports `PREEMPT_DYNAMIC` in uname even on the rt variant).
for i in $(seq 1 15); do
  if ssh_run 'grep -q PREEMPT_RT /proc/version' 2>/dev/null; then
    echo "RT kernel active after ${i} polls"; break
  fi
  sleep 12
done

# 5) /etc/hosts entry — the OCP route hostname uses .gcp.local baseDomain
#    which has no public DNS. Pin the apps wildcard to the apps LB IP.
ssh_run "sudo sed -i '/${APPS_LB_IP}/d' /etc/hosts; echo '${APPS_LB_IP} kafka-cluster-kafka-bootstrap-ulysses-demo.apps.ulysses-demo.kavara.gcp.local' | sudo tee -a /etc/hosts"

# 6) Push mTLS material. Cluster CA verifies brokers; user.crt/user.key auths
#    us as User:CN=$KAFKA_USER. (KafkaUser secret's own ca.crt is the
#    clients-CA — broker side, not what the client needs.)
TMP=$(mktemp -d)
oc get secret "$KAFKA_CLUSTER_CA_SECRET" -n "$NAMESPACE" -o jsonpath='{.data.ca\.crt}' | base64 -d > "$TMP/ca.crt"
oc get secret "$KAFKA_USER" -n "$NAMESPACE" -o jsonpath='{.data.user\.crt}' | base64 -d > "$TMP/user.crt"
oc get secret "$KAFKA_USER" -n "$NAMESPACE" -o jsonpath='{.data.user\.key}' | base64 -d > "$TMP/user.key"
ssh_run 'sudo mkdir -p /etc/kafka-certs && sudo chmod 0755 /etc/kafka-certs'
gcloud compute scp --zone="$ZONE" --project="$PROJECT" "$TMP/ca.crt" "$TMP/user.crt" "$TMP/user.key" "$VM":/tmp/
ssh_run 'sudo mv /tmp/ca.crt /tmp/user.crt /tmp/user.key /etc/kafka-certs/ && sudo chown root:root /etc/kafka-certs/* && sudo chmod 0644 /etc/kafka-certs/ca.crt /etc/kafka-certs/user.crt && sudo chmod 0600 /etc/kafka-certs/user.key'
rm -rf "$TMP"

# 7) Push bench.py.
gcloud compute scp --zone="$ZONE" --project="$PROJECT" "$SCRIPT_DIR/bench.py" "$VM":/home/$(whoami)/bench.py

cat <<EOF

ulysses-rt-bench is provisioned. To run the 30-min benchmark:

  gcloud compute ssh $VM --zone=$ZONE --project=$PROJECT
  KAFKA_BOOTSTRAP=$KAFKA_BOOTSTRAP \\
    DURATION_SECONDS=1800 \\
    OUT_CSV=results.csv \\
    sudo -E taskset -c 2-7 chrt -f 80 \$HOME/.local/bin/python3.11 ~/bench.py

  # Fetch results
  gcloud compute scp --zone=$ZONE --project=$PROJECT $VM:results.csv .

Cleanup:
  $0 teardown
EOF
