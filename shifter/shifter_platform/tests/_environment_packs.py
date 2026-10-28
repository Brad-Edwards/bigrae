"""Source and image address spaces for Shifter-owned environment packs (#2550).

Every Shifter-owned pack has one canonical source directory under
``shifter/environment-packs/``. The portal Dockerfile copies only the shipped
packs to their runtime destinations under the image app root. Persisted
``package_ref`` values name those runtime destinations, not checkout paths, so
tests that register the shipped manifest stage the packs into an image-shaped
root through the same Dockerfile copy rules.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

SHIFTER_DIR = Path(__file__).resolve().parents[2]
ENVIRONMENT_PACKS_ROOT = SHIFTER_DIR / "environment-packs"
PORTAL_DOCKERFILE = SHIFTER_DIR / "shifter_platform" / "Dockerfile"

# `COPY [--flags] environment-packs/<pack> <runtime destination>` in the
# production stage. The source is relative to the `./shifter` build context and
# the destination is relative to WORKDIR /app (the RAES package root).
_PACK_COPY = re.compile(r"^COPY\s+(?:--\S+\s+)*(environment-packs/\S+)\s+(\S+)\s*$", re.MULTILINE)


def shipped_pack_copies() -> dict[str, Path]:
    """Return ``{runtime package_ref: canonical source dir}`` from the Dockerfile."""
    copies: dict[str, Path] = {}
    for source, destination in _PACK_COPY.findall(PORTAL_DOCKERFILE.read_text(encoding="utf-8")):
        copies[destination.rstrip("/")] = SHIFTER_DIR / source.rstrip("/")
    return copies


def stage_shipped_packs(root: Path) -> Path:
    """Copy every shipped pack to its runtime destination under ``root``."""
    for package_ref, source in shipped_pack_copies().items():
        shutil.copytree(source, root / package_ref)
    return root
