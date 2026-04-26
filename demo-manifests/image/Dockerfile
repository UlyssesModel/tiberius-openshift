# Ulysses SOR demo consumer image.
#
# Intentionally a "stub" build: real SOR + Ulysses normalized-EBM code is
# air-gapped per Kavara policy. This image demonstrates the pipeline shape
# with a textbook von-Neumann-entropy-of-a-density-matrix calculation
# that Jarett can swap for the real Ulysses pipeline in production.
#
# Base: Red Hat UBI9 Python 3.11 — redistributable, OpenShift-friendly,
# rootless by default.

FROM registry.access.redhat.com/ubi9/python-311:latest

LABEL org.opencontainers.image.title="Ulysses SOR demo consumer"
LABEL org.opencontainers.image.vendor="Kavara, Inc."
LABEL org.opencontainers.image.source="https://github.com/UlyssesModel/tiberius-openshift"
LABEL org.opencontainers.image.licenses="Apache-2.0"

# UBI9 python images run as uid 1001 ("default") — OpenShift's arbitrary UID
# policy treats this as safe. Stay non-root throughout.
USER 0

# librdkafka is shipped as a binary wheel dep of confluent-kafka, so we do
# NOT need to dnf-install librdkafka-devel. Keep the image small.
RUN dnf install -y --nodocs \
        numactl \
        util-linux \
    && dnf clean all \
    && rm -rf /var/cache/dnf

WORKDIR /opt/app

COPY requirements.txt /opt/app/requirements.txt
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r /opt/app/requirements.txt

COPY consumer.py /opt/app/consumer.py
COPY healthz.py /opt/app/healthz.py

# Back to non-root for the run.
USER 1001

# Prometheus metrics + healthz / readyz endpoints.
EXPOSE 9100

# OpenMP defaults match the KIRK study's calibrated GNR+TDX production
# recommendation (32-thread close placement). These can be overridden
# via the ConfigMap at deploy time without a rebuild.
ENV OMP_NUM_THREADS=32 \
    OMP_PROC_BIND=close \
    OMP_PLACES=cores \
    MALLOC_ARENA_MAX=4 \
    PYTHONUNBUFFERED=1

ENTRYPOINT ["python", "/opt/app/consumer.py"]
