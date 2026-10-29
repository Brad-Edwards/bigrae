"""Chart admission policies on a real Kubernetes API server (#2651).

The other admission tests evaluate the rendered CEL with cel-python, which does
not type-check expressions or parse escapes the way the API server does. This
module applies the rendered policies to a real kube-apiserver of the GKE
Kubernetes minor and creates provisioner Jobs in server-side dry-run mode. A
policy that does not compile, or that denies the canonical Job, then fails CI
instead of a deploy.

CI starts the server with ``scripts/admission_apiserver/start.sh`` and sets
``ADMISSION_KUBECONFIG``. The tests carry the ``apiserver`` marker; the unit
lanes exclude it.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from collections.abc import Callable
from typing import Any

import pytest

from tests.platform.test_gcp_job_launcher_manifests import (
    CHART_DIR,
    JOBS_NAMESPACE,
    PROVISIONER_LAUNCHER_USERNAME,
    _canonical_admission_job,
    _canonical_admission_parameters,
    _load_helm_documents,
    _load_yaml_documents,
    _malformed_admission_job,
)

pytestmark = pytest.mark.apiserver

PROFILES = ("values-gcp-dev.yaml", "values-gcp-prod.yaml", "values-aws-dev.yaml", "values-aws-prod.yaml")
ADMISSION_KINDS = ("ValidatingAdmissionPolicy", "ValidatingAdmissionPolicyBinding")
Kubectl = Callable[..., subprocess.CompletedProcess[str]]


@pytest.fixture(scope="module")
def kubectl() -> Kubectl:
    kubeconfig = os.environ.get("ADMISSION_KUBECONFIG")
    if not kubeconfig:
        pytest.skip("ADMISSION_KUBECONFIG is not set; the admission CI job starts the API server")
    binary = os.environ.get("ADMISSION_KUBECTL") or shutil.which("kubectl")
    if binary is None:
        pytest.fail("kubectl is required for the API server admission tests")

    def run(*args: str, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(  # noqa: S603
            [binary, "--kubeconfig", kubeconfig, *args], input=stdin, capture_output=True, text=True, check=False
        )

    run("create", "namespace", JOBS_NAMESPACE)
    # RBAC runs before admission, so the impersonated launcher needs Job create rights.
    run(
        "create",
        "clusterrolebinding",
        "admission-test-launcher",
        "--clusterrole=cluster-admin",
        f"--user={PROVISIONER_LAUNCHER_USERNAME}",
    )
    return run


def _render(values_file: str) -> list[dict[str, Any]]:
    helm = shutil.which("helm")
    if helm is None:
        pytest.fail("helm is required to render the chart")
    rendered = subprocess.run(  # noqa: S603
        [helm, "template", "shifter", str(CHART_DIR), "-f", str(CHART_DIR / values_file)],
        check=True,
        capture_output=True,
        text=True,
    )
    return _load_yaml_documents(rendered.stdout)


def _apply_admission(kubectl: Kubectl, documents: list[dict[str, Any]]) -> None:
    policies = [document for document in documents if document.get("kind") in ADMISSION_KINDS]
    assert policies, "the chart renders no admission policy"
    applied = kubectl(
        "apply",
        "--server-side",
        "--force-conflicts",
        "--field-manager=admission-test",
        "-f",
        "-",
        stdin=json.dumps({"apiVersion": "v1", "kind": "List", "items": policies}),
    )
    assert applied.returncode == 0, applied.stderr


def _create_job(kubectl: Kubectl, job: dict[str, Any], *, username: str) -> subprocess.CompletedProcess[str]:
    return kubectl(
        "create",
        "--dry-run=server",
        f"--as={username}",
        "--namespace",
        JOBS_NAMESPACE,
        "-f",
        "-",
        stdin=json.dumps(_submittable(job)),
    )


def _submittable(job: dict[str, Any]) -> dict[str, Any]:
    """Add the type fields and the name that only the API server needs to the shared fixture."""
    metadata = {"name": "pulumi-provisioner-0123456789abcdef", **job["metadata"]}
    return {"apiVersion": "batch/v1", "kind": "Job", **job, "metadata": metadata}


def _denied_by_policy(result: subprocess.CompletedProcess[str]) -> bool:
    return result.returncode != 0 and "ValidatingAdmissionPolicy 'restrict-provisioner-jobs'" in result.stderr


@pytest.mark.parametrize("values_file", PROFILES)
def test_rendered_admission_policies_compile_on_the_api_server(kubectl: Kubectl, values_file: str) -> None:
    # The API server rejects a policy whose CEL does not parse or type-check.
    _apply_admission(kubectl, _render(values_file))


def test_api_server_admits_the_canonical_provisioner_job_and_denies_violations(kubectl: Kubectl) -> None:
    _apply_admission(kubectl, _load_helm_documents(runtime_env=_canonical_admission_parameters()))
    canonical = _canonical_admission_job()

    # The API server activates a policy change asynchronously, and an earlier
    # test applied other parameters. Wait until this version enforces: it admits
    # the canonical Job and denies a Job that only the policy rejects.
    probe = _malformed_admission_job(canonical, "operation")
    deadline = time.monotonic() + 60
    while True:
        admitted = _create_job(kubectl, canonical, username=PROVISIONER_LAUNCHER_USERNAME)
        if admitted.returncode == 0 and _denied_by_policy(
            _create_job(kubectl, probe, username=PROVISIONER_LAUNCHER_USERNAME)
        ):
            break
        assert time.monotonic() < deadline, admitted.stderr or "the policy did not deny the probe Job"
        time.sleep(2)

    for mutation in ("image-pull-policy", "literal-tamper", "volume", "secret", "extra-arg"):
        denied = _create_job(
            kubectl, _malformed_admission_job(canonical, mutation), username=PROVISIONER_LAUNCHER_USERNAME
        )
        assert _denied_by_policy(denied), f"{mutation}: {denied.stderr or denied.stdout}"
