"""Structural contract for the one-shot S8 cutover Job."""

import importlib.util
from pathlib import Path

import yaml

MODULE_PATH = Path(__file__).resolve().parents[1] / "render_identity_cutover_job.py"
SPEC = importlib.util.spec_from_file_location("render_identity_cutover_job", MODULE_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
render = MODULE.render


def _manifest() -> str:
    deployment = {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": "portal-web", "namespace": "shifter-platform"},
        "spec": {
            "template": {
                "spec": {
                    "serviceAccountName": "portal",
                    "automountServiceAccountToken": False,
                    "restartPolicy": "Always",
                    "containers": [
                        {
                            "name": "portal",
                            "image": "example.invalid/portal@sha256:" + "1" * 64,
                            "env": [{"name": "OPENFGA_MODEL_ID", "value": "model"}],
                            "volumeMounts": [{"name": "openfga-token", "mountPath": "/run/secrets/openfga"}],
                            "readinessProbe": {"httpGet": {"path": "/health/", "port": 8000}},
                        }
                    ],
                    "volumes": [{"name": "openfga-token", "secret": {"secretName": "openfga"}}],
                }
            }
        },
    }
    return yaml.safe_dump(deployment)


def test_cutover_job_reuses_exact_runtime_binding_and_mounts_facts_as_files() -> None:
    job = render(_manifest(), job_name="identity-cutover-42-1")
    pod = job["spec"]["template"]["spec"]
    container = pod["containers"][0]
    assert pod["restartPolicy"] == "Never"
    assert pod["serviceAccountName"] == "portal"
    assert {item["name"] for item in container["env"]} == {
        "OPENFGA_MODEL_ID",
        "SHIFTER_IDENTITY_MAINTENANCE",
    }
    assert {item["name"] for item in pod["volumes"]} == {
        "openfga-token",
        "identity-cutover-source",
        "identity-bootstrap-source",
        "identity-cutover-facts",
        "identity-bootstrap-facts",
    }
    sources = {item["name"]: item["secret"] for item in pod["volumes"] if "secret" in item}
    assert sources["identity-cutover-source"] == {
        "secretName": "identity-cutover-facts",
        "optional": True,
        "defaultMode": 0o440,
    }
    assert sources["identity-bootstrap-source"] == {
        "secretName": "identity-bootstrap-facts",
        "optional": True,
        "defaultMode": 0o440,
    }
    assert pod["securityContext"]["fsGroup"] == 1000
    assert pod["initContainers"][0]["name"] == "identity-facts-prepare"
    assert "install -m 600" in pod["initContainers"][0]["args"][0]
    script = container["args"][2]
    assert "identity_cutover status" in script
    assert script.index("identity_cutover status") < script.index("identity_cutover apply")
    assert '.get("plan_digest", "")' in script
    assert script.index('.get("plan_digest", "")') < script.index("identity_cutover plan")
    assert 'if [ -z "$digest" ]' in script
    assert "identity_cutover apply" in script
    assert "identity_cutover_bootstrap" in script
    assert "identity_cutover verify" in script
    assert script.index("identity_cutover apply") < script.index("identity_cutover_bootstrap")
    assert script.index("identity_cutover_bootstrap") < script.index("identity_cutover verify")
    assert "identity_runtime_check" in script
    assert "readinessProbe" not in container


def test_status_probe_uses_runtime_binding_without_cutover_facts() -> None:
    job = render(_manifest(), job_name="identity-cutover-status-42-1", status_only=True)
    pod = job["spec"]["template"]["spec"]
    container = pod["containers"][0]
    assert container["args"][2] == "python manage.py identity_cutover status\npython manage.py identity_runtime_check\n"
    assert {item["name"] for item in pod["volumes"]} == {"openfga-token"}
    assert "initContainers" not in pod
