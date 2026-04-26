#!/usr/bin/env bash
# Build + push the ulysses-sor demo image.
# Requires: podman or docker, a Quay account (or internal registry) login.
#
# Env vars:
#   REGISTRY        default quay.io/kavara
#   TAG             default demo-v1
#   BUILDER         default podman (falls back to docker)
#   PLATFORM        default linux/amd64  (TDX needs amd64)
set -euo pipefail

REGISTRY="${REGISTRY:-quay.io/kavara}"
TAG="${TAG:-demo-v1}"
PLATFORM="${PLATFORM:-linux/amd64}"
BUILDER="${BUILDER:-$(command -v podman >/dev/null && echo podman || echo docker)}"
IMAGE="${REGISTRY}/ulysses-sor:${TAG}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${SCRIPT_DIR}"

echo ">> building ${IMAGE} with ${BUILDER} for ${PLATFORM}"
"${BUILDER}" build --platform "${PLATFORM}" -t "${IMAGE}" .

echo ">> pushing ${IMAGE}"
"${BUILDER}" push "${IMAGE}"

echo ">> done. reference in 05-ulysses-consumer.yaml as:"
echo "     image: ${IMAGE}"
