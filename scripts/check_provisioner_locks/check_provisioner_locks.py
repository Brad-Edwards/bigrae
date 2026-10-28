#!/usr/bin/env python3
"""Fail when the provisioner image locks disagree with uv.lock (#2610).

The provisioner Dockerfile installs from the hash-pinned ``requirements.lock``
and ``requirements-gcp.lock``, not from ``uv.lock``. A fix in ``uv.lock`` that
is not exported does not reach the image. This check requires that:

- ``requirements.lock`` equals ``uv export --no-emit-project --frozen --no-dev``;
- each package in ``requirements-gcp.lock`` that ``uv.lock`` also resolves has
  the same version.

The regeneration procedure is in docs/architecture/rev1-build-deployment-provenance.md.
"""

from __future__ import annotations

import re
import subprocess  # nosec B404
import sys
import tomllib
from pathlib import Path

PROVISIONER = Path(__file__).resolve().parents[2] / "shifter" / "engine" / "provisioner"
_PIN_RE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==([^\s\;]+)", re.MULTILINE)


def _normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _locked_versions(uv_lock: Path) -> dict[str, str]:
    data = tomllib.loads(uv_lock.read_text(encoding="utf-8"))
    return {_normalize(p["name"]): p["version"] for p in data.get("package", []) if "version" in p}


def check(provisioner: Path = PROVISIONER) -> list[str]:
    """Return one message for each disagreement."""
    errors: list[str] = []
    exported = subprocess.run(  # nosec B603 B607 - fixed argv
        ["uv", "export", "--no-emit-project", "--frozen", "--no-dev", "--quiet"],
        cwd=provisioner,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    committed = (provisioner / "requirements.lock").read_text(encoding="utf-8")
    # The first two lines are the generator header, which names the exact command.
    if exported.split("\n", 2)[2:] != committed.split("\n", 2)[2:]:
        errors.append("requirements.lock does not match uv.lock; re-export it")
    locked = _locked_versions(provisioner / "uv.lock")
    gcp = (provisioner / "requirements-gcp.lock").read_text(encoding="utf-8")
    for name, version in _PIN_RE.findall(gcp):
        expected = locked.get(_normalize(name))
        if expected is not None and expected != version:
            errors.append(f"requirements-gcp.lock pins {name}=={version}, but uv.lock resolves {expected}")
    return errors


def main() -> int:
    errors = check()
    for message in errors:
        print(f"provisioner locks: {message}", file=sys.stderr)
    if errors:
        print("Regenerate per docs/architecture/rev1-build-deployment-provenance.md", file=sys.stderr)
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
