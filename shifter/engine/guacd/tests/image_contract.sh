#!/bin/bash
# Contract test for the Shifter guacd image (#1698).
#
# Builds shifter/engine/guacd/Dockerfile and checks the behavior that the image
# adds to the upstream guacd image:
#   1. the image runs as the non-root guacd user;
#   2. the RDP drive directory exists, is owned by guacd, has mode 750, and the
#      image user can write to it;
#   3. guacd starts and accepts TCP connections on 4822 under the Kubernetes
#      runtime posture (read-only root, all capabilities dropped).
#
# Usage: image_contract.sh [image-tag]
set -euo pipefail

TAG="${1:-shifter-guacd:contract-test}"
CONTEXT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
NAME="guacd-contract-$$"
DRIVE=/var/lib/guacamole/drive

# shellcheck disable=SC2329  # invoked by the EXIT trap below
cleanup() {
  docker rm -f "${NAME}" >/dev/null 2>&1 || true
}
trap cleanup EXIT

fail() {
  echo "guacd-contract: FAIL $*" >&2
  exit 1
}

docker build --quiet --tag "${TAG}" "${CONTEXT}" >/dev/null

user="$(docker run --rm --entrypoint id "${TAG}" -un)"
[[ "${user}" == "guacd" ]] || fail "image user is '${user}', expected guacd"

drive="$(docker run --rm --entrypoint stat "${TAG}" -c '%U %a' "${DRIVE}")"
[[ "${drive}" == "guacd 750" ]] || fail "drive directory is '${drive}', expected 'guacd 750'"

docker run --rm --entrypoint sh "${TAG}" -c "touch ${DRIVE}/probe && rm ${DRIVE}/probe" \
  || fail "the image user cannot write to ${DRIVE}"

uid="$(docker run --rm --entrypoint id "${TAG}" -u)"
gid="$(docker run --rm --entrypoint id "${TAG}" -g)"
docker run --detach --name "${NAME}" --read-only --cap-drop ALL \
  --tmpfs /tmp --tmpfs "${DRIVE}:uid=${uid},gid=${gid},mode=0750" "${TAG}" >/dev/null

for _ in $(seq 1 30); do
  if docker exec "${NAME}" nc -z 127.0.0.1 4822; then
    echo "guacd-contract: OK"
    exit 0
  fi
  sleep 1
done
docker logs "${NAME}" >&2 || true
fail "guacd did not accept connections on 4822 within 30 seconds"
