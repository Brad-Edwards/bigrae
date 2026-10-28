#!/usr/bin/env python3
"""Render a release manifest with effect producers stopped for identity cutover."""

from __future__ import annotations

import argparse
from pathlib import Path

import yaml

_POLICY_DEPLOYMENTS = frozenset({"openfga", "openfga-admin"})
_NAMESPACES = frozenset({"shifter-platform", "shifter-jobs"})
_PROBE_KINDS = frozenset({"Namespace", "ServiceAccount", "ConfigMap", "Secret"})
_PROBE_NETWORK_POLICIES = frozenset(
    f"allow-{lane}-{purpose}-egress"
    for lane in ("platform", "jobs")
    for purpose in ("dns", "metadata-server", "google-apis", "provider-apis", "native-sql")
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
        if kind == "HorizontalPodAutoscaler":
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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--probe-prerequisites", action="store_true")
    args = parser.parse_args()
    manifest = args.manifest.read_text(encoding="utf-8")
    if args.probe_prerequisites:
        args.output.write_text(probe_prerequisites(manifest), encoding="utf-8")
        return 0
    rendered, stopped = quiesce(manifest)
    args.output.write_text(rendered, encoding="utf-8")
    print(f"Prepared maintenance manifest with {len(stopped)} stopped effect producers.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
