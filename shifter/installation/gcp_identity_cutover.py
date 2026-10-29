"""Identity cutover projections from the canonical release chart only."""

from __future__ import annotations

import copy
import re
from typing import Any

import yaml

_DNS_LABEL = re.compile(r"[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?")
_SCRIPT = """\
status="$(python manage.py identity_cutover status)"
state="$(printf '%s' "$status" | python -c 'import json,sys; print(json.load(sys.stdin)["state"])')"
if [ "$state" = activated ]; then
  python manage.py identity_runtime_check
  exit 0
fi
test -f /run/identity-cutover/cutover.json
test -f /run/identity-bootstrap/bootstrap.json
digest="$(printf '%s' "$status" | python -c 'import json,sys; print(json.load(sys.stdin).get("plan_digest", ""))')"
if [ -z "$digest" ]; then
  plan="$(python manage.py identity_cutover plan --facts /run/identity-cutover/cutover.json)"
  digest="$(printf '%s' "$plan" | python -c 'import json,sys; print(json.load(sys.stdin)["plan_digest"])')"
fi
python manage.py identity_cutover apply --facts /run/identity-cutover/cutover.json --plan-digest "$digest"
python manage.py identity_cutover_bootstrap --facts /run/identity-bootstrap/bootstrap.json --plan-digest "$digest"
python manage.py identity_cutover verify --facts /run/identity-cutover/cutover.json --plan-digest "$digest"
python manage.py identity_runtime_check
"""
_STATUS_SCRIPT = """\
result="$(python manage.py identity_cutover status --probe)"
printf 'SHIFTER_IDENTITY_CUTOVER_RESULT=%s\\n' "$result"
"""


def _is_portal_deployment(item: object) -> bool:
    """Select only the canonical portal Deployment in its owning namespace."""
    return (
        isinstance(item, dict)
        and item.get("kind") == "Deployment"
        and item.get("metadata", {}).get("name") == "portal-web"
        and item.get("metadata", {}).get("namespace") == "shifter-platform"
    )


def _portal_template(manifest: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """Clone one complete runtime binding and validate its sole portal container."""
    deployments = [item for item in yaml.safe_load_all(manifest) if _is_portal_deployment(item)]
    if len(deployments) != 1:
        raise ValueError("Release manifest must contain exactly one portal-web Deployment")
    pod = copy.deepcopy(deployments[0]["spec"]["template"]["spec"])
    containers = pod.get("containers", [])
    portal = next((item for item in containers if item.get("name") == "portal"), None)
    if portal is None or len(containers) != 1:
        raise ValueError("Portal workload shape is not eligible for cutover")
    return pod, portal


def render(manifest: str, *, job_name: str, status_only: bool = False) -> dict[str, object]:
    """Clone the exact portal database/policy binding into a bounded Job."""
    if _DNS_LABEL.fullmatch(job_name) is None:
        raise ValueError("Cutover Job name must be a bounded DNS label")
    pod, portal = _portal_template(manifest)
    pod["restartPolicy"] = "Never"
    pod.pop("terminationGracePeriodSeconds", None)
    portal["args"] = ["/bin/sh", "-ceu", _STATUS_SCRIPT if status_only else _SCRIPT]
    for field in ("ports", "readinessProbe", "livenessProbe", "startupProbe", "lifecycle"):
        portal.pop(field, None)
    env = portal.setdefault("env", [])
    env.append({"name": "SHIFTER_IDENTITY_MAINTENANCE", "value": "true"})
    if not status_only:
        _mount_private_facts(pod, portal)
    labels = {"app.kubernetes.io/part-of": "shifter", "app.kubernetes.io/component": "identity-cutover"}
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {"name": job_name, "namespace": "shifter-platform", "labels": labels},
        "spec": {
            "backoffLimit": 0,
            "activeDeadlineSeconds": 1800,
            "template": {"metadata": {"labels": labels}, "spec": pod},
        },
    }


def _mount_private_facts(pod: dict[str, Any], portal: dict[str, Any]) -> None:
    """Copy projected ceremony Secrets into private files owned by the image user."""
    mounts = portal.setdefault("volumeMounts", [])
    mounts.extend(
        [
            {"name": "identity-cutover-facts", "mountPath": "/run/identity-cutover", "readOnly": True},
            {"name": "identity-bootstrap-facts", "mountPath": "/run/identity-bootstrap", "readOnly": True},
        ]
    )
    pod_security = pod.setdefault("securityContext", {})
    pod_security["fsGroup"] = 1000
    pod_security["fsGroupChangePolicy"] = "OnRootMismatch"
    prepare = {
        "name": "identity-facts-prepare",
        "image": portal["image"],
        "command": ["/bin/sh", "-ceu"],
        "args": [
            "for pair in cutover bootstrap; do "
            'source="/run/identity-${pair}-source/${pair}.json"; '
            'target="/run/identity-${pair}/${pair}.json"; '
            'if [ -f "$source" ]; then install -m 600 "$source" "$target"; fi; '
            "done"
        ],
        "securityContext": copy.deepcopy(portal.get("securityContext", {})),
        "resources": copy.deepcopy(portal.get("resources", {})),
        "volumeMounts": [
            {"name": "identity-cutover-source", "mountPath": "/run/identity-cutover-source", "readOnly": True},
            {"name": "identity-bootstrap-source", "mountPath": "/run/identity-bootstrap-source", "readOnly": True},
            {"name": "identity-cutover-facts", "mountPath": "/run/identity-cutover"},
            {"name": "identity-bootstrap-facts", "mountPath": "/run/identity-bootstrap"},
        ],
    }
    if "imagePullPolicy" in portal:
        prepare["imagePullPolicy"] = portal["imagePullPolicy"]
    pod["initContainers"] = [prepare, *pod.get("initContainers", [])]
    volumes = pod.setdefault("volumes", [])
    volumes.extend(
        [
            {
                "name": "identity-cutover-source",
                "secret": {"secretName": "identity-cutover-facts", "optional": True, "defaultMode": 0o440},
            },
            {
                "name": "identity-bootstrap-source",
                "secret": {"secretName": "identity-bootstrap-facts", "optional": True, "defaultMode": 0o440},
            },
            {"name": "identity-cutover-facts", "emptyDir": {"sizeLimit": "1Mi"}},
            {"name": "identity-bootstrap-facts", "emptyDir": {"sizeLimit": "1Mi"}},
        ]
    )


_POLICY_DEPLOYMENTS = frozenset({"openfga", "openfga-admin"})
_NAMESPACES = frozenset({"shifter-platform", "shifter-jobs"})
_PROBE_KINDS = frozenset({"Namespace", "ServiceAccount", "ConfigMap", "Secret"})
_PROBE_NETWORK_POLICIES = frozenset(
    f"allow-{lane}-{purpose}-egress"
    for lane in ("platform", "jobs")
    # native-sql is the IAM database path (TCP 3307); private-service is the
    # password-authentication database path (TCP 5432, #2669).
    for purpose in ("dns", "metadata-server", "google-apis", "provider-apis", "native-sql", "private-service")
)


def probe_prerequisites(manifest: str) -> str:
    """Install pod references without changing any running workload or reconciler."""
    documents = [
        document
        for document in yaml.safe_load_all(manifest)
        if isinstance(document, dict)
        and (
            document.get("kind") in _PROBE_KINDS
            or (
                document.get("kind") == "NetworkPolicy"
                and document.get("metadata", {}).get("namespace") in _NAMESPACES
                and document.get("metadata", {}).get("name") in _PROBE_NETWORK_POLICIES
            )
        )
    ]
    return yaml.safe_dump_all(documents, sort_keys=False)


def quiesce(manifest: str) -> tuple[str, tuple[str, ...]]:
    """Stop release Deployments/CronJobs and omit autoscalers deterministically."""
    documents: list[dict[str, object]] = []
    stopped: list[str] = []
    for document in yaml.safe_load_all(manifest):
        if not isinstance(document, dict):
            continue
        metadata = document.get("metadata", {})
        namespace = metadata.get("namespace") if isinstance(metadata, dict) else None
        if namespace not in _NAMESPACES:
            documents.append(document)
            continue
        kind = document.get("kind")
        name = str(metadata.get("name", ""))
        if kind in {"HorizontalPodAutoscaler", "Job"}:
            continue
        if kind == "Deployment" and name not in _POLICY_DEPLOYMENTS:
            document.setdefault("spec", {})["replicas"] = 0
            stopped.append(f"{namespace}/deployment/{name}")
        elif kind == "CronJob":
            document.setdefault("spec", {})["suspend"] = True
            stopped.append(f"{namespace}/cronjob/{name}")
        documents.append(document)
    if "shifter-platform/deployment/portal-web" not in stopped:
        raise ValueError("Release manifest does not contain the portal effect producer")
    rendered = "\n---\n".join(yaml.safe_dump(item, sort_keys=False).rstrip() for item in documents) + "\n"
    return rendered, tuple(sorted(stopped))
