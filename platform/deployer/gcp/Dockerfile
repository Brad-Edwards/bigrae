# syntax=docker/dockerfile:1

# GitHub-less GCP deployer image (#2571, ADR-070).
#
# The image is the execution unit of one published release. It holds the deploy
# and destroy commands, the installation package, the GCP scripts, the Terraform
# roots and modules, the Helm chart, the smoke script, and the pinned tools. A
# deploy that runs this image builds no images, needs no repository checkout, and
# downloads nothing from GitHub.
#
# Build context: the repository root. Dockerfile.dockerignore next to this file
# limits the context to the deploy tooling.
#
# Required build inputs:
#   --build-arg SOURCE_REVISION=<40-hex commit SHA of the release>
#   --build-context release=<directory that holds release.json>
# The release.json file is the bigrae.gcp-release.v1 manifest of the same
# revision. The build stops when the manifest is missing, is not valid, or names
# a different revision.
#
# Supply chain (ADR-037): every FROM is digest-pinned (R2). Every downloaded tool
# is verified against a SHA-256 pinned in this reviewed file before it runs (R3).
# A pin comes from the vendor's published checksum for that exact version; it is
# not fetched from the download origin at build time, so a compromised origin can
# not supply both a tampered archive and a matching checksum. Python dependencies install from the frozen uv lock with hash
# enforcement and wheels only (R4). Terraform verifies each provider package
# against the HashiCorp signature and the committed lock file.

# ---------------------------------------------------------------------------
# Release manifest. The release workflow replaces this stage with
# --build-context release=<directory>. A build without that context stops here.
# ---------------------------------------------------------------------------
# base image: python:3.12-slim-trixie
FROM python@sha256:423ed6ab25b1921a477529254bfeeabf5855151dc2c3141699a1bfc852199fbf AS release
RUN echo "Supply the release manifest: --build-context release=<directory with release.json>" >&2 && exit 1

# ---------------------------------------------------------------------------
# Google Cloud CLI. The official image holds a self-contained SDK with its own
# bundled Python. Only the SDK directory is copied into the runtime image.
# ---------------------------------------------------------------------------
# base image: gcr.io/google.com/cloudsdktool/google-cloud-cli:591.0.0-slim
FROM gcr.io/google.com/cloudsdktool/google-cloud-cli@sha256:98c1ee595323614b2047419ac313080c65c033d166eca1f8d642ea6925d47a25 AS gcloud

# ---------------------------------------------------------------------------
# Python dependencies. The bootstrap lock covers every third-party package that
# the commands and the GCP scripts import: the commands start the scripts with
# the same interpreter. The first-party installation package is not installed
# here; it has no external resolution to lock or hash (same as the platform
# image). A .pth file puts it on the interpreter path, so a child process with a
# clean environment can still import it.
# ---------------------------------------------------------------------------
# base image: python3.12-trixie-slim (uv)
FROM ghcr.io/astral-sh/uv@sha256:3137a0b606f65a74ee0245f43dae219b09e8af98fc37fef20841cbceef35a646 AS python-deps
WORKDIR /src
COPY scripts/bootstrap/pyproject.toml scripts/bootstrap/uv.lock scripts/bootstrap/
COPY shifter/installation/pyproject.toml shifter/installation/
RUN uv export --project scripts/bootstrap --frozen --no-dev --no-emit-project \
        --no-emit-package installation --no-header --output-file /tmp/requirements.txt && \
    uv venv /opt/bigrae/venv --python /usr/local/bin/python3 && \
    uv pip install --python /opt/bigrae/venv/bin/python --no-cache --no-config \
        --require-hashes --only-binary :all: -r /tmp/requirements.txt && \
    echo "/opt/bigrae/lib" > /opt/bigrae/venv/lib/python3.12/site-packages/bigrae-lib.pth

# ---------------------------------------------------------------------------
# Pinned tools. Each download is checked against the SHA-256 below. Bump a
# version and its SHA-256 together, from the vendor's published checksum.
# ---------------------------------------------------------------------------
# base image: python:3.12-slim-trixie
FROM python@sha256:423ed6ab25b1921a477529254bfeeabf5855151dc2c3141699a1bfc852199fbf AS tools
SHELL ["/bin/bash", "-o", "pipefail", "-c"]
# Keep each tool at the latest release of its line (Terraform 1.17, Helm 3,
# kubectl 1.34, Trivy, ORAS 1.3). Older releases carry fixable HIGH and CRITICAL
# findings in their compiled Go dependencies.
ARG TERRAFORM_VERSION=1.17.1
# From terraform_1.17.1_SHA256SUMS (signature checked with HashiCorp key C874011F0AB405110D02105534365D9472D7468F).
ARG TERRAFORM_SHA256=4638628058e71a55212b1c563712eac03b902128f40740f3dc8174df12135061
ARG HELM_VERSION=v3.22.1
ARG HELM_SHA256=896cf63684a42a723d61c980369d6e588330927deb13676ca0d85006dd6da0ea
ARG KUBECTL_VERSION=v1.34.13
ARG KUBECTL_SHA256=b4bb494e9466a7a94bb851414548bc0d3e2f8da90757e6eccc3371e648648591
ARG TRIVY_VERSION=0.76.1
ARG TRIVY_SHA256=c7cbe3137fb8a48a17574bede3dd6beb16742d1e9772929940fc90082c39d9c0
ARG ORAS_VERSION=1.3.6
ARG ORAS_SHA256=66fb0ad7206f3f6fa77ed5b552049d364ccf4b50b1684554f731f76302b52ca0
RUN apt-get update && \
    apt-get install -y --no-install-recommends ca-certificates curl unzip && \
    apt-get clean && rm -rf /var/lib/apt/lists/*
WORKDIR /downloads
RUN fetch() { curl --proto '=https' --retry 5 --retry-delay 3 --retry-all-errors --connect-timeout 20 -fsSL -o "$1" "$2"; } && \
    mkdir -p /out/bin && \
    fetch "terraform_${TERRAFORM_VERSION}_linux_amd64.zip" \
      "https://releases.hashicorp.com/terraform/${TERRAFORM_VERSION}/terraform_${TERRAFORM_VERSION}_linux_amd64.zip" && \
    echo "${TERRAFORM_SHA256}  terraform_${TERRAFORM_VERSION}_linux_amd64.zip" | sha256sum -c - && \
    unzip -j "terraform_${TERRAFORM_VERSION}_linux_amd64.zip" terraform -d /out/bin && \
    fetch "helm-${HELM_VERSION}-linux-amd64.tar.gz" "https://get.helm.sh/helm-${HELM_VERSION}-linux-amd64.tar.gz" && \
    echo "${HELM_SHA256}  helm-${HELM_VERSION}-linux-amd64.tar.gz" | sha256sum -c - && \
    tar -xzf "helm-${HELM_VERSION}-linux-amd64.tar.gz" --strip-components=1 -C /out/bin linux-amd64/helm && \
    fetch kubectl "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/amd64/kubectl" && \
    echo "${KUBECTL_SHA256}  kubectl" | sha256sum -c - && \
    install -m 0755 kubectl /out/bin/kubectl && \
    fetch "trivy_${TRIVY_VERSION}_Linux-64bit.tar.gz" \
      "https://github.com/aquasecurity/trivy/releases/download/v${TRIVY_VERSION}/trivy_${TRIVY_VERSION}_Linux-64bit.tar.gz" && \
    echo "${TRIVY_SHA256}  trivy_${TRIVY_VERSION}_Linux-64bit.tar.gz" | sha256sum -c - && \
    tar -xzf "trivy_${TRIVY_VERSION}_Linux-64bit.tar.gz" -C /out/bin trivy && \
    fetch "oras_${ORAS_VERSION}_linux_amd64.tar.gz" \
      "https://github.com/oras-project/oras/releases/download/v${ORAS_VERSION}/oras_${ORAS_VERSION}_linux_amd64.tar.gz" && \
    echo "${ORAS_SHA256}  oras_${ORAS_VERSION}_linux_amd64.tar.gz" | sha256sum -c - && \
    tar -xzf "oras_${ORAS_VERSION}_linux_amd64.tar.gz" -C /out/bin oras && \
    chmod 0755 /out/bin/* && \
    rm -rf /downloads/*

# ---------------------------------------------------------------------------
# Terraform provider mirror. Each root initializes against its committed lock
# file (read only), and Terraform checks the HashiCorp signature of each
# provider package. The runtime image then installs providers only from this
# mirror.
# ---------------------------------------------------------------------------
FROM tools AS providers
COPY platform/terraform/gcp /opt/bigrae/platform/terraform/gcp
RUN find /opt/bigrae/platform/terraform/gcp -name .terraform.lock.hcl -printf '%h\n' | sort > /opt/bigrae/terraform-roots && \
    test -s /opt/bigrae/terraform-roots && \
    while IFS= read -r root; do \
      TF_DATA_DIR="$(mktemp -d)" && export TF_DATA_DIR && \
      /out/bin/terraform -chdir="${root}" init -backend=false -input=false -lockfile=readonly -no-color >/dev/null && \
      /out/bin/terraform -chdir="${root}" providers mirror -platform=linux_amd64 /opt/bigrae/terraform-providers && \
      rm -rf "${TF_DATA_DIR}" || exit 1; \
    done < /opt/bigrae/terraform-roots

# ---------------------------------------------------------------------------
# Build gke-gcloud-auth-plugin with fixed Go, x/net, and x/text dependencies so
# the deployer scan needs no plugin-specific exceptions. CVE-2026-56857 affects
# Windows only; the other Go and module findings apply to the vendor's Linux
# binary. The Go image and upstream source commit are pinned, and go.sum
# verifies each module. Keep both pins current. Return to the Google binary
# when it passes the deployer scan without plugin-specific exceptions.
# ---------------------------------------------------------------------------
# base image: golang:1.27.3-trixie
FROM golang@sha256:c65ecfe613b539ac74c62a4c3b2c27e8f8a6076384c8f27e8a3c0f3be17cc385 AS gke-auth-plugin
SHELL ["/bin/bash", "-o", "pipefail", "-c"]
# The commit that tag v37.1.13 points to.
ARG CLOUD_PROVIDER_GCP_COMMIT=8ee7ffa8eec98eb6e9a560a5276131516da1e359
WORKDIR /src
RUN git init -q . && \
    git fetch -q --depth 1 https://github.com/kubernetes/cloud-provider-gcp.git "${CLOUD_PROVIDER_GCP_COMMIT}" && \
    git checkout -q FETCH_HEAD && \
    test "$(git rev-parse HEAD)" = "${CLOUD_PROVIDER_GCP_COMMIT}" && \
    CGO_ENABLED=0 GOTOOLCHAIN=local go build -buildvcs=false -trimpath -ldflags "-s -w" \
      -o /out/gke-gcloud-auth-plugin ./cmd/gke-gcloud-auth-plugin && \
    /out/gke-gcloud-auth-plugin --help >/dev/null

# ---------------------------------------------------------------------------
# Runtime image.
# ---------------------------------------------------------------------------
# base image: python:3.12-slim-trixie
FROM python@sha256:423ed6ab25b1921a477529254bfeeabf5855151dc2c3141699a1bfc852199fbf AS deployer
SHELL ["/bin/bash", "-o", "pipefail", "-c"]
ARG SOURCE_REVISION=""
ARG SOURCE_REPOSITORY_URL=""
ARG GCLOUD_VERSION=591.0.0
LABEL maintainer="Shifter maintainers" \
      org.opencontainers.image.title="bigrae-gcp-deployer" \
      org.opencontainers.image.description="GitHub-less GCP deploy, release-scan, smoke, and destroy commands for one BigRAE release" \
      org.opencontainers.image.source="${SOURCE_REPOSITORY_URL}" \
      org.opencontainers.image.revision="${SOURCE_REVISION}" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.authors="Shifter maintainers"

# Apply the Debian security updates, add jq for the smoke script, and create the
# non-root user. openssl and bash come with the base image.
RUN apt-get update && \
    apt-get upgrade -y && \
    apt-get install -y --no-install-recommends ca-certificates jq && \
    apt-get clean && rm -rf /var/lib/apt/lists/* && \
    groupadd --gid 10001 bigrae && \
    useradd --uid 10001 --gid bigrae --create-home --shell /bin/bash bigrae

COPY --from=gcloud /usr/lib/google-cloud-sdk /opt/google-cloud-sdk
COPY --from=gke-auth-plugin /out/gke-gcloud-auth-plugin /opt/google-cloud-sdk/bin/gke-gcloud-auth-plugin

# Remove pip from the base Python and from the bundled Python of the SDK. The
# image installs its dependencies at build time and runs no package manager.
# The vendored packages of these pip copies carry known findings.
RUN python3 -m pip uninstall --yes --no-input pip && \
    find /opt/google-cloud-sdk/platform/bundledpythonunix -depth -path '*/site-packages/*' -type d \
      \( -name pip -o -name 'pip-*.dist-info' \) -exec rm -rf {} + && \
    rm -f /opt/google-cloud-sdk/platform/bundledpythonunix/bin/pip* && \
    test -z "$(find /usr/local/lib /opt/google-cloud-sdk/platform/bundledpythonunix -path '*/site-packages/pip' -print -quit)"
COPY --from=tools /out/bin/ /usr/local/bin/
COPY --from=python-deps /opt/bigrae/venv /opt/bigrae/venv
COPY --from=providers /opt/bigrae/terraform-providers /opt/bigrae/terraform-providers
COPY --from=providers /opt/bigrae/terraform-roots /opt/bigrae/terraform-roots

# Deploy tooling, in the repository layout. get_repo_root() in
# scripts/bootstrap/bootstrap_core.py resolves to /opt/bigrae.
COPY shifter/installation /opt/bigrae/lib/installation
COPY shifter/shifter_platform/shared/model_access /opt/bigrae/lib/shared/model_access
COPY shifter/shifter_platform/shared/mission_control_lease /opt/bigrae/lib/shared/mission_control_lease
COPY shifter/shifter_platform/shared/gcp_range_telemetry /opt/bigrae/lib/shared/gcp_range_telemetry
COPY shifter/shifter_platform/shared/gcp_native_identity /opt/bigrae/lib/shared/gcp_native_identity
COPY scripts/bootstrap /opt/bigrae/scripts/bootstrap
COPY scripts/gcp /opt/bigrae/scripts/gcp
COPY scripts/check_tf_gcp_wif_trust/_resolved_plan.py /opt/bigrae/scripts/check_tf_gcp_wif_trust/_resolved_plan.py
COPY scripts/smoke-test-gcp.sh /opt/bigrae/scripts/smoke-test-gcp.sh
COPY platform/terraform/gcp /opt/bigrae/platform/terraform/gcp
COPY platform/charts/shifter /opt/bigrae/platform/charts/shifter
COPY .trivyignore.yaml /opt/bigrae/.trivyignore.yaml
COPY platform/deployer/gcp/terraform.tfrc /opt/bigrae/terraform.tfrc
COPY --from=release release.json /opt/bigrae/release.json

ENV PATH=/opt/bigrae/venv/bin:/opt/google-cloud-sdk/bin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
    HOME=/home/bigrae \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TF_CLI_CONFIG_FILE=/opt/bigrae/terraform.tfrc \
    CHECKPOINT_DISABLE=1 \
    CLOUDSDK_COMPONENT_MANAGER_DISABLE_UPDATE_CHECK=1 \
    BIGRAE_RELEASE_MANIFEST=/opt/bigrae/release.json \
    BIGRAE_SOURCE_REVISION=${SOURCE_REVISION}

# Drop privileges. A numeric UID:GID lets an admission controller with
# runAsNonRoot verify the user without a name lookup. Any user can use the
# working directory, so an operator can also run the image with --user and the
# own gcloud credentials of that user.
USER 10001:10001
WORKDIR /tmp

# Build checks, with no network:
# - the gcloud SDK version equals GCLOUD_VERSION;
# - the release manifest is valid and names SOURCE_REVISION;
# - each Terraform root initializes from the provider mirror, with no lock file
#   change and no write to the image;
# - the command line loads.
RUN --network=none \
    test "$(cat /opt/google-cloud-sdk/VERSION)" = "${GCLOUD_VERSION}" && \
    python -c 'import os, sys; from pathlib import Path; from installation.gcp_release import load_release_manifest; manifest = load_release_manifest(Path(os.environ["BIGRAE_RELEASE_MANIFEST"])); sys.exit(None if manifest.source_revision == os.environ["BIGRAE_SOURCE_REVISION"] else "the release manifest names another source revision")' && \
    while IFS= read -r root; do \
      TF_DATA_DIR="$(mktemp -d)" && export TF_DATA_DIR && \
      terraform -chdir="${root}" init -backend=false -input=false -lockfile=readonly -no-color >/dev/null && \
      rm -rf "${TF_DATA_DIR}" || exit 1; \
    done < /opt/bigrae/terraform-roots && \
    python /opt/bigrae/scripts/bootstrap/deploy.py gcp-deploy --help >/dev/null

# The deployer runs one command and exits. It is not a service, so it has no
# health check.
HEALTHCHECK NONE

ENTRYPOINT ["/opt/bigrae/venv/bin/python", "/opt/bigrae/scripts/bootstrap/deploy.py"]
CMD ["--help"]
