#!/usr/bin/env python3
"""Render the one-shot identity cutover Job from the release portal pod."""

from __future__ import annotations

import argparse
from pathlib import Path

import yaml
from installation.gcp_identity_cutover import render


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
