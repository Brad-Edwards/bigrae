#!/usr/bin/env python3
"""Stop effect producers and prove their pods terminated before SQL conversion."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import time
from pathlib import Path

_NAMESPACES = ("shifter-platform", "shifter-jobs")
_INVENTORIES = ("cronjob", "deployment", "job", "horizontalpodautoscaler", "replicaset", "pod")
_POLICY = frozenset({("shifter-platform", "openfga"), ("shifter-platform", "openfga-admin")})
_NAME = re.compile(r"[a-z0-9](?:[-a-z0-9.]*[a-z0-9])?\Z")


def _kubectl(*args: str) -> str:
    return subprocess.run(  # nosec B603 B607 - fixed kubectl, validated resource names, no shell
        ["kubectl", *args], check=True, capture_output=True, text=True, timeout=960
    ).stdout


def _inventory(kind: str, namespace: str) -> list[dict]:
    raw = json.loads(_kubectl("get", kind, "-n", namespace, "-o", "json", "--request-timeout=30s"))
    if not isinstance(raw, dict) or not isinstance(raw.get("items"), list):
        raise ValueError("Invalid Kubernetes inventory")
    rows = raw["items"]
    seen = set()
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("metadata"), dict):
            raise ValueError("Invalid Kubernetes inventory row")
        metadata = row["metadata"]
        name, uid = metadata.get("name"), metadata.get("uid")
        if (
            not isinstance(name, str)
            or not _NAME.fullmatch(name)
            or not isinstance(uid, str)
            or not uid
            or uid in seen
            or metadata.get("namespace", namespace) != namespace
        ):
            raise ValueError("Invalid Kubernetes resource identity")
        seen.add(uid)
        if not isinstance(row.get("status", {}), dict):
            raise ValueError("Invalid Kubernetes resource status")
        if kind == "job":
            active = row.get("status", {}).get("active", 0)
            if type(active) is not int or active < 0:
                raise ValueError("Invalid Kubernetes job activity")
        if kind in {"pod", "replicaset"}:
            owners = metadata.get("ownerReferences", [])
            if not isinstance(owners, list) or any(
                not isinstance(owner, dict)
                or not all(isinstance(owner.get(field), str) and owner[field] for field in ("kind", "name", "uid"))
                for owner in owners
            ):
                raise ValueError("Invalid Kubernetes owner identity")
        if kind == "pod" and row.get("status", {}).get("phase") not in {
            "Pending",
            "Running",
            "Succeeded",
            "Failed",
            "Unknown",
        }:
            raise ValueError("Invalid Kubernetes pod phase")
    return rows


def _policy_pod(pod: dict, replicasets: list[dict], deployments: list[dict], namespace: str) -> bool:
    deployment_uids = {row["metadata"]["uid"] for row in deployments if (namespace, row["metadata"]["name"]) in _POLICY}
    replica_uids = {
        row["metadata"]["uid"]
        for row in replicasets
        if any(
            owner.get("controller") is True and owner["kind"] == "Deployment" and owner["uid"] in deployment_uids
            for owner in row["metadata"].get("ownerReferences", [])
        )
    }
    return any(
        owner.get("controller") is True and owner["kind"] == "ReplicaSet" and owner["uid"] in replica_uids
        for owner in pod["metadata"].get("ownerReferences", [])
    )


def enter_maintenance(manifest: Path, *, timeout_seconds: float = 600) -> None:
    # Fully retrieve and validate every namespace before the first mutation.
    inventories = {(namespace, kind): _inventory(kind, namespace) for namespace in _NAMESPACES for kind in _INVENTORIES}
    for namespace in _NAMESPACES:
        for row in inventories[namespace, "horizontalpodautoscaler"]:
            _kubectl("delete", f"horizontalpodautoscaler/{row['metadata']['name']}", "-n", namespace, "--wait=true")
    for namespace in _NAMESPACES:
        for row in inventories[namespace, "cronjob"]:
            _kubectl(
                "patch",
                f"cronjob/{row['metadata']['name']}",
                "-n",
                namespace,
                "--type=merge",
                "-p",
                '{"spec":{"suspend":true}}',
            )
        for row in inventories[namespace, "deployment"]:
            name = row["metadata"]["name"]
            if (namespace, name) not in _POLICY:
                _kubectl("scale", f"deployment/{name}", "-n", namespace, "--replicas=0")
                _kubectl("rollout", "status", f"deployment/{name}", "-n", namespace, "--timeout=10m")
    # A CronJob can have scheduled another Job after the initial snapshot but
    # before suspension. Re-read it; failure must never become an empty list.
    for namespace in _NAMESPACES:
        for row in _inventory("job", namespace):
            if row.get("status", {}).get("active", 0) > 0:
                _kubectl(
                    "wait",
                    "--for=condition=complete",
                    f"job/{row['metadata']['name']}",
                    "-n",
                    namespace,
                    "--timeout=15m",
                )
    _wait_application_pods(timeout_seconds)
    # Disabled broker manifests restore broader shared egress policies. Never
    # expose old broker/proxy pods to those additive grants before termination.
    _kubectl("apply", "-f", str(manifest))


def _wait_application_pods(timeout_seconds: float) -> None:
    deadline = time.monotonic() + timeout_seconds
    while True:
        active = []
        for namespace in _NAMESPACES:
            deployments = _inventory("deployment", namespace)
            replicasets = _inventory("replicaset", namespace)
            for pod in _inventory("pod", namespace):
                if pod["status"]["phase"] not in {"Succeeded", "Failed"} and not _policy_pod(
                    pod, replicasets, deployments, namespace
                ):
                    active.append((namespace, pod["metadata"]["uid"]))
        if not active:
            return
        # deletionTimestamp only begins termination. It is never proof that
        # an old application process or native SQL proxy has stopped.
        if time.monotonic() >= deadline:
            raise TimeoutError("Effect-producing pods did not terminate before identity conversion")
        time.sleep(min(2, max(0, deadline - time.monotonic())))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    args = parser.parse_args()
    enter_maintenance(args.manifest)
    print("Identity maintenance verified: application pods are terminal or absent.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
