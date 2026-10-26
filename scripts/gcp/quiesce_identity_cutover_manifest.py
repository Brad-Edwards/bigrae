#!/usr/bin/env python3
"""Render a release manifest with effect producers stopped for identity cutover."""

from __future__ import annotations

import argparse
from pathlib import Path

import yaml

_POLICY_DEPLOYMENTS = frozenset({"openfga", "openfga-admin"})
_NAMESPACES = frozenset({"shifter-platform", "shifter-jobs"})


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
    args = parser.parse_args()
    rendered, stopped = quiesce(args.manifest.read_text(encoding="utf-8"))
    args.output.write_text(rendered, encoding="utf-8")
    print(f"Prepared maintenance manifest with {len(stopped)} stopped effect producers.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
