"""Contract tests for the S8 maintenance manifest."""

import importlib.util
from pathlib import Path

import yaml

MODULE_PATH = Path(__file__).resolve().parents[1] / "quiesce_identity_cutover_manifest.py"
SPEC = importlib.util.spec_from_file_location("quiesce_identity_cutover_manifest", MODULE_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
quiesce = MODULE.quiesce


def _document(kind: str, name: str, namespace: str = "shifter-platform") -> dict:
    return {"apiVersion": "v1", "kind": kind, "metadata": {"name": name, "namespace": namespace}, "spec": {}}


def test_quiesced_release_stops_effects_without_stopping_policy_or_retaining_hpas() -> None:
    source = yaml.safe_dump_all(
        [
            _document("Deployment", "portal-web"),
            _document("Deployment", "worker-engine"),
            _document("Deployment", "openfga"),
            _document("Deployment", "openfga-admin"),
            _document("CronJob", "cleanup"),
            _document("HorizontalPodAutoscaler", "portal-web"),
        ]
    )
    rendered, stopped = quiesce(source)
    documents = list(yaml.safe_load_all(rendered))
    by_name = {(item["kind"], item["metadata"]["name"]): item for item in documents}
    assert by_name[("Deployment", "portal-web")]["spec"]["replicas"] == 0
    assert by_name[("Deployment", "worker-engine")]["spec"]["replicas"] == 0
    assert "replicas" not in by_name[("Deployment", "openfga")]["spec"]
    assert "replicas" not in by_name[("Deployment", "openfga-admin")]["spec"]
    assert by_name[("CronJob", "cleanup")]["spec"]["suspend"] is True
    assert not any(item["kind"] == "HorizontalPodAutoscaler" for item in documents)
    assert stopped == (
        "shifter-platform/cronjob/cleanup",
        "shifter-platform/deployment/portal-web",
        "shifter-platform/deployment/worker-engine",
    )
