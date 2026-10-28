"""Vite build-manifest resolution for the SPA host.

The SPA (#1300 / #1302) is built by Vite at image-build time into Django's
static tree with content-hashed filenames and a per-build manifest.
``collectstatic`` folds that tree into ``STATIC_ROOT`` and WhiteNoise
fingerprints it. A host
template cannot know the hashed entry filename ahead of time, so it resolves the
entry through this helper: read the Vite manifest to find the logical asset
paths, then pass them through Django ``static()`` so the WhiteNoise manifest
returns the final URL.

The resolver tolerates a missing manifest (returns empty URLs and logs a
warning) so the host still renders when the bundle has not been built yet (for
example in unit tests that exercise a view without a frontend build).
"""

from __future__ import annotations

import json
import logging
from functools import lru_cache
from pathlib import Path

from django.conf import settings
from django.templatetags.static import static

logger = logging.getLogger(__name__)

# Vite emits its build manifest at ``<outDir>/.vite/manifest.json``. The SPA
# build outputs to ``<static>/spa`` (frontend/vite.config.ts).
_SPA_STATIC_PREFIX = "spa"

# Default entry (matches the Vite project's rollup input).
DEFAULT_ENTRY = "src/main.tsx"


def _manifest_path(static_prefix: str) -> Path | None:
    """Return the on-disk Vite manifest path from the first static source dir."""
    dirs = list(getattr(settings, "STATICFILES_DIRS", ()))
    if not dirs:
        return None
    return Path(dirs[0]) / static_prefix / ".vite" / "manifest.json"


def _read_manifest(static_prefix: str) -> dict[str, object]:
    """Load and parse a Vite manifest, or return an empty mapping if absent."""
    path = _manifest_path(static_prefix)
    if path is None or not path.is_file():
        logger.warning("Vite manifest not found (looked at %s); host will render without assets", path)
        return {}
    try:
        with path.open(encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        logger.warning("Vite manifest at %s could not be read/parsed", path, exc_info=True)
        return {}
    return data if isinstance(data, dict) else {}


@lru_cache(maxsize=8)
def _cached_manifest(static_prefix: str) -> dict[str, object]:
    """Cache the parsed manifest per prefix for the process lifetime (production)."""
    return _read_manifest(static_prefix)


def _load_manifest(static_prefix: str) -> dict[str, object]:
    """Return the Vite manifest, re-reading each call under DEBUG for dev iteration."""
    if getattr(settings, "DEBUG", False):
        return _read_manifest(static_prefix)
    return _cached_manifest(static_prefix)


def vite_asset_urls(entry: str = DEFAULT_ENTRY, *, static_prefix: str = _SPA_STATIC_PREFIX) -> dict[str, object]:
    """Resolve a Vite entry to static URLs for its JS module and CSS files.

    Returns ``{"js": <url or None>, "css": [<url>, ...]}``. When the manifest or
    the entry is missing, ``js`` is ``None`` and ``css`` is empty so the caller
    renders without asset tags rather than raising.
    """
    manifest = _load_manifest(static_prefix)
    chunk = manifest.get(entry)
    if not isinstance(chunk, dict):
        return {"js": None, "css": []}
    js_file = chunk.get("file")
    js_url = static(f"{static_prefix}/{js_file}") if isinstance(js_file, str) else None
    css_files = chunk.get("css") or []
    css_urls = [static(f"{static_prefix}/{name}") for name in css_files if isinstance(name, str)]
    return {"js": js_url, "css": css_urls}
