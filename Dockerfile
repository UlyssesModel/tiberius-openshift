# Copyright 2026 Kavara. Licensed under the Apache License, Version 2.0.
#
# SmartOrderRouter HTTP service — reference container image
#
# Build:
#   docker build -t kavara/sor-service:reference .
# Run locally:
#   docker run -p 8080:8080 -e KAVARA_HARDWARE_ID=gnr-tdx kavara/sor-service:reference
# Push to OpenShift internal registry:
#   oc registry login
#   docker tag kavara/sor-service:reference $(oc registry info)/kavara-sor/sor-service:v1.0.0
#   docker push $(oc registry info)/kavara-sor/sor-service:v1.0.0

FROM registry.access.redhat.com/ubi9/python-312:latest

WORKDIR /app

# --- Dependencies (cached layer)
COPY requirements.txt ./
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

# --- Application code
COPY sor_router.py sor_service.py ./

# --- Non-root user (OpenShift SCC-compliant)
USER 1001

EXPOSE 8080

LABEL org.opencontainers.image.title="Kavara SmartOrderRouter (reference)" \
      org.opencontainers.image.description="Reference HTTP service demonstrating the two-layer compute-dispatch pattern" \
      org.opencontainers.image.source="https://github.com/UlyssesModel/tiberius-openshift" \
      org.opencontainers.image.licenses="Apache-2.0" \
      org.opencontainers.image.vendor="Kavara" \
      kavara.ai/component="smart-order-router" \
      kavara.ai/adr="ADR-006"

# Single worker by design — SOR in-process state is per-process.
# Scale via Deployment replicas, not uvicorn workers.
CMD ["uvicorn", "sor_service:app", \
     "--host", "0.0.0.0", \
     "--port", "8080", \
     "--workers", "1", \
     "--access-log"]
