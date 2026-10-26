#!/usr/bin/env python3
"""Render the one-shot identity cutover Job from the release portal pod."""

from __future__ import annotations

import argparse
import copy
import re
from pathlib import Path

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
python manage.py identity_cutover status
python manage.py identity_runtime_check
"""


def render(manifest: str, *, job_name: str, status_only: bool = False) -> dict[str, object]:
    """Clone the exact portal database/policy binding into a bounded Job."""
    if _DNS_LABEL.fullmatch(job_name) is None:
        raise ValueError("Cutover Job name must be a bounded DNS label")
    deployments = [
        item
        for item in yaml.safe_load_all(manifest)
        if isinstance(item, dict)
        and item.get("kind") == "Deployment"
        and item.get("metadata", {}).get("name") == "portal-web"
        and item.get("metadata", {}).get("namespace") == "shifter-platform"
    ]
    if len(deployments) != 1:
        raise ValueError("Release manifest must contain exactly one portal-web Deployment")
    pod = copy.deepcopy(deployments[0]["spec"]["template"]["spec"])
    containers = pod.get("containers", [])
    portal = next((item for item in containers if item.get("name") == "portal"), None)
    if portal is None or len(containers) != 1:
        raise ValueError("Portal workload shape is not eligible for cutover")
    pod["restartPolicy"] = "Never"
    pod.pop("terminationGracePeriodSeconds", None)
    portal["args"] = ["/bin/sh", "-ceu", _STATUS_SCRIPT if status_only else _SCRIPT]
    for field in ("ports", "readinessProbe", "livenessProbe", "startupProbe", "lifecycle"):
        portal.pop(field, None)
    env = portal.setdefault("env", [])
    env.append({"name": "SHIFTER_IDENTITY_MAINTENANCE", "value": "true"})
    if not status_only:
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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--job-name", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--status-only", action="store_true")
    args = parser.parse_args()
    document = render(args.manifest.read_text(encoding="utf-8"), job_name=args.job_name, status_only=args.status_only)
    args.output.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
