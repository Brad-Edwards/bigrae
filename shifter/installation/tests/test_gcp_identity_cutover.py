"""Pure native cutover projections from the one canonical release chart."""

import pytest
import yaml

from installation.gcp_identity_cutover import probe_prerequisites, quiesce, render


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
    assert "identity_cutover status --probe" in container["args"][2]
    assert "SHIFTER_IDENTITY_CUTOVER_RESULT=" in container["args"][2]
    assert {item["name"] for item in pod["volumes"]} == {"openfga-token"}
    assert "initContainers" not in pod


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


def test_probe_prerequisites_never_apply_workloads_or_autoscalers() -> None:
    source = yaml.safe_dump_all(
        [
            _document(kind, kind.lower())
            for kind in (
                "Namespace",
                "ServiceAccount",
                "ConfigMap",
                "Secret",
                "Deployment",
                "CronJob",
                "Job",
                "HorizontalPodAutoscaler",
                "StatefulSet",
                "DaemonSet",
            )
        ]
    )
    documents = list(yaml.safe_load_all(probe_prerequisites(source)))
    assert {item["kind"] for item in documents} == {"Namespace", "ServiceAccount", "ConfigMap", "Secret"}


def test_probe_includes_only_existing_network_prerequisites_before_starting_job() -> None:
    source = yaml.safe_dump_all(
        [
            _document("NetworkPolicy", "allow-platform-native-sql-egress"),
            _document("NetworkPolicy", "allow-jobs-native-sql-egress", "shifter-jobs"),
            _document("NetworkPolicy", "allow-platform-dns-egress"),
            _document("NetworkPolicy", "allow-platform-metadata-server-egress"),
            _document("NetworkPolicy", "allow-platform-google-apis-egress"),
            _document("NetworkPolicy", "allow-platform-provider-apis-egress"),
            _document("NetworkPolicy", "allow-platform-private-service-egress"),
            _document("NetworkPolicy", "allow-jobs-private-service-egress", "shifter-jobs"),
            _document("NetworkPolicy", "unrelated-policy"),
            _document("NetworkPolicy", "allow-platform-native-sql-egress", "unrelated-namespace"),
            _document("Deployment", "portal-web"),
        ]
    )
    documents = list(yaml.safe_load_all(probe_prerequisites(source)))
    assert [(row["metadata"]["namespace"], row["metadata"]["name"]) for row in documents] == [
        ("shifter-platform", "allow-platform-native-sql-egress"),
        ("shifter-jobs", "allow-jobs-native-sql-egress"),
        ("shifter-platform", "allow-platform-dns-egress"),
        ("shifter-platform", "allow-platform-metadata-server-egress"),
        ("shifter-platform", "allow-platform-google-apis-egress"),
        ("shifter-platform", "allow-platform-provider-apis-egress"),
        ("shifter-platform", "allow-platform-private-service-egress"),
        ("shifter-jobs", "allow-jobs-private-service-egress"),
    ]


@pytest.mark.parametrize("name", ["", "Upper", "x" * 64, "bad/name"])
def test_cutover_job_names_are_bounded_dns_labels(name):
    with pytest.raises(ValueError, match="bounded DNS label"):
        render(_manifest(), job_name=name)


@pytest.mark.parametrize("shape", ["missing", "duplicate", "other-container", "extra-container"])
def test_cutover_rejects_unknown_portal_shapes(shape):
    doc = yaml.safe_load(_manifest())
    if shape == "missing":
        doc["metadata"]["name"] = "other"
    if shape == "other-container":
        doc["spec"]["template"]["spec"]["containers"][0]["name"] = "other"
    if shape == "extra-container":
        doc["spec"]["template"]["spec"]["containers"].append({"name": "other"})
    manifest = yaml.safe_dump_all([doc, doc] if shape == "duplicate" else [doc])
    with pytest.raises(ValueError):
        render(manifest, job_name="cutover-test")


def test_quiescence_requires_the_actual_portal_and_keeps_other_namespaces():
    with pytest.raises(ValueError, match="portal effect producer"):
        quiesce(yaml.safe_dump(_document("Deployment", "other")))
    other = _document("Deployment", "external", "another-namespace")
    other["spec"]["replicas"] = 2
    rendered, _ = quiesce(yaml.safe_dump_all([other, _document("Deployment", "portal-web")]))
    assert (
        next(doc for doc in yaml.safe_load_all(rendered) if doc["metadata"]["name"] == "external")["spec"]["replicas"]
        == 2
    )
