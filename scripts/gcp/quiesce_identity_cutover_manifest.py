#!/usr/bin/env python3
"""Render a release manifest with effect producers stopped for identity cutover."""

from __future__ import annotations

import argparse
from pathlib import Path

from installation.gcp_identity_cutover import probe_prerequisites, quiesce


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
