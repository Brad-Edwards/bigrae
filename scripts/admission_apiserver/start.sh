#!/usr/bin/env bash
# Start a local etcd and kube-apiserver from the envtest bundle (#2651).
#
# The admission-policy tests apply the rendered chart policies to a real API
# server, which compiles and type-checks their CEL as GKE and EKS do. The server
# runs with token authentication and RBAC, binds to 127.0.0.1 only, and is torn
# down with the CI runner. It writes a kubeconfig to <work dir>/kubeconfig.
#
# Usage: start.sh <envtest bin dir> <work dir>
set -euo pipefail

bin_dir=$1
work=$2
mkdir -p "${work}/certs"

"${bin_dir}/etcd" \
  --data-dir "${work}/etcd" \
  --listen-client-urls http://127.0.0.1:2379 \
  --advertise-client-urls http://127.0.0.1:2379 \
  --listen-peer-urls http://127.0.0.1:2380 \
  >"${work}/etcd.log" 2>&1 &

openssl genrsa -out "${work}/sa.key" 2048 2>/dev/null
openssl rsa -in "${work}/sa.key" -pubout -out "${work}/sa.pub" 2>/dev/null
token=$(openssl rand -hex 24)
printf '%s,admission-test-admin,admission-test-admin,system:masters\n' "${token}" >"${work}/tokens.csv"

"${bin_dir}/kube-apiserver" \
  --etcd-servers=http://127.0.0.1:2379 \
  --bind-address=127.0.0.1 \
  --advertise-address=127.0.0.1 \
  --secure-port=6443 \
  --cert-dir="${work}/certs" \
  --service-cluster-ip-range=10.0.0.0/24 \
  --service-account-issuer=https://kubernetes.default.svc \
  --service-account-key-file="${work}/sa.pub" \
  --service-account-signing-key-file="${work}/sa.key" \
  --token-auth-file="${work}/tokens.csv" \
  --authorization-mode=RBAC \
  >"${work}/kube-apiserver.log" 2>&1 &

cat >"${work}/kubeconfig" <<KUBECONFIG
apiVersion: v1
kind: Config
clusters:
- name: admission-test
  cluster:
    server: https://127.0.0.1:6443
    insecure-skip-tls-verify: true
users:
- name: admission-test-admin
  user:
    token: ${token}
contexts:
- name: admission-test
  context:
    cluster: admission-test
    user: admission-test-admin
current-context: admission-test
KUBECONFIG
chmod 600 "${work}/kubeconfig"

for _ in $(seq 1 60); do
  if "${bin_dir}/kubectl" --kubeconfig "${work}/kubeconfig" get --raw /readyz >/dev/null 2>&1; then
    echo "kube-apiserver is ready"
    exit 0
  fi
  sleep 1
done
echo "kube-apiserver did not become ready" >&2
tail -n 40 "${work}/kube-apiserver.log" >&2
exit 1
