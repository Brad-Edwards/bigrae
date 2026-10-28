"""Portal container image invariants."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

from packaging.markers import Marker

REPO_ROOT = Path(__file__).resolve().parents[4]
DOCKERFILE = REPO_ROOT / "shifter" / "shifter_platform" / "Dockerfile"


def test_cloud_lock_preserves_platform_versions_for_container_python() -> None:
    """A second cloud install must not downgrade shared runtime dependencies."""
    platform = DOCKERFILE.parent
    packages = tomllib.loads((platform / "uv.lock").read_text())["package"]
    environment = {"python_version": "3.12", "python_full_version": "3.12.0", "sys_platform": "linux"}
    versions = {
        package["name"]: package["version"]
        for package in packages
        if not package.get("resolution-markers")
        or any(Marker(marker).evaluate(environment) for marker in package["resolution-markers"])
    }
    cloud = dict(re.findall(r"^([\w-]+)==([^\s]+)", (platform / "requirements-gcp.lock").read_text(), re.MULTILINE))
    mismatches = {
        name: (version, versions[name])
        for name, version in cloud.items()
        if name in versions and version != versions[name]
    }
    assert not mismatches, f"Cloud lock replaces platform dependencies: {mismatches}"


def test_portal_image_creates_owned_appuser_home() -> None:
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")

    assert "--create-home --home-dir /home/appuser" in dockerfile
    assert "HOME=/home/appuser" in dockerfile
    assert "/home/appuser/.terraform.d/plugin-cache" in dockerfile
    assert "/home/appuser/.pulumi" in dockerfile
    assert "chown -R appuser:appgroup /app/staticfiles /app/media /home/appuser" in dockerfile


def test_portal_image_builds_django_artifacts_as_appuser() -> None:
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")

    user_index = dockerfile.index("USER appuser")
    compile_index = dockerfile.index("python manage.py compilemessages")
    collect_index = dockerfile.index("python manage.py collectstatic --noinput")
    healthcheck_index = dockerfile.index("HEALTHCHECK")

    assert user_index < compile_index < collect_index < healthcheck_index
    assert "BUILD_DJANGO_SECRET_KEY=" in dockerfile
    assert "BUILD_FIELD_ENCRYPTION_KEY=" in dockerfile
    assert 'DJANGO_SECRET_KEY="$BUILD_DJANGO_SECRET_KEY"' in dockerfile
    assert 'FIELD_ENCRYPTION_KEY="$BUILD_FIELD_ENCRYPTION_KEY"' in dockerfile
    assert "OIDC_RP_CLIENT_ID=build-time-client" in dockerfile
    assert "DJANGO_DEBUG=True" not in dockerfile


def test_portal_image_ships_exactly_the_bootstrap_manifest_packs() -> None:
    """Explicit pack copies agree with the in-box manifest; no other pack ships (#2550)."""
    from cms.scenarios.inbox import SHIPPED_INBOX_MANIFEST, load_inbox_manifest
    from tests._environment_packs import ENVIRONMENT_PACKS_ROOT, shipped_pack_copies

    copies = shipped_pack_copies()
    manifest_refs = {request.package_ref for request in load_inbox_manifest(SHIPPED_INBOX_MANIFEST)}
    assert set(copies) == manifest_refs
    for package_ref, source in copies.items():
        assert source.parent == ENVIRONMENT_PACKS_ROOT
        assert source.name == Path(package_ref).name
        assert (source / "pack.yaml").is_file()

    dockerfile = DOCKERFILE.read_text(encoding="utf-8")
    assert not re.search(r"^COPY\s+(?:--\S+\s+)*environment-packs/?\s", dockerfile, re.MULTILINE)
    for package_ref in copies:
        assert f"COPY --chown=appuser:appgroup environment-packs/{Path(package_ref).name} {package_ref}\n" in dockerfile
    development_packs = {path.name for path in ENVIRONMENT_PACKS_ROOT.iterdir() if path.is_dir()} - {
        source.name for source in copies.values()
    }
    assert development_packs == {"preparation-http-smoke", "shifter-raes-validation"}


def test_environment_packs_have_one_canonical_source() -> None:
    """Retired pack locations hold no maintained copies (#2550)."""
    from cms.scenarios.inbox import SHIPPED_INBOX_MANIFEST

    retired = [REPO_ROOT / "scenario-dev", *(path for path in SHIPPED_INBOX_MANIFEST.parent.iterdir() if path.is_dir())]
    assert [str(path.relative_to(REPO_ROOT)) for path in retired if path.exists()] == []
