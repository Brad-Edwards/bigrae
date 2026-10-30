"""Tests for the GCP release manifest (``installation.gcp_release``, #2571)."""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from installation.cli import main
from installation.errors import InstallationConfigError
from installation.gcp_release import (
    RELEASE_IMAGE_NAMES,
    build_release_manifest,
    load_release_manifest,
    parse_release_manifest,
    render_release_manifest,
)

REVISION = "0123456789abcdef0123456789abcdef01234567"
REGISTRY = "us-central1-docker.pkg.dev/acme-release/bigrae-release"


def _images() -> dict[str, str]:
    return {name: f"{REGISTRY}/{name}@sha256:{str(index) * 64}" for index, name in enumerate(RELEASE_IMAGE_NAMES, 1)}


def _manifest_payload(**overrides: object) -> dict[str, object]:
    return {
        "schema": "bigrae.gcp-release.v1",
        "version": "1.4.0",
        "source_revision": REVISION,
        "chart_version": "0.2.0",
        "images": _images(),
        **overrides,
    }


class TestReleaseInstallInputs:
    """#2679: a release pins its base range images and carries its authorization model."""

    def test_base_images_and_model_round_trip(self, tmp_path: Path):
        base = {role: f"ghcr.io/acme/shifter-gce-{role}@sha256:" + "a" * 64 for role in ("kali", "ubuntu", "dc")}
        model = {"schema_version": "1.1", "type_definitions": [{"type": "principal"}]}
        manifest = build_release_manifest(
            version="1.4.0",
            source_revision=REVISION,
            chart_version="0.2.0",
            images=_images(),
            base_images=base,
            authorization_model=model,
        )
        path = tmp_path / "release.json"
        path.write_text(render_release_manifest(manifest), encoding="utf-8")
        loaded = load_release_manifest(path)
        assert loaded.base_images == base and loaded.authorization_model == model

    @pytest.mark.parametrize(
        "overrides",
        [
            {"base_images": {"kali": "ghcr.io/acme/shifter-gce-kali@sha256:" + "a" * 64}},
            {"base_images": dict.fromkeys(("kali", "ubuntu", "dc"), "ghcr.io/acme/x:latest")},
            {"authorization_model": {"type_definitions": []}},
        ],
    )
    def test_partial_or_mutable_install_inputs_are_rejected(self, overrides):
        with pytest.raises(InstallationConfigError):
            parse_release_manifest(json.dumps(_manifest_payload(**overrides)))


class TestReleaseManifest:
    def test_round_trip_is_canonical_and_exposes_digests(self, tmp_path: Path):
        manifest = build_release_manifest(
            version="1.4.0", source_revision=REVISION, chart_version="0.2.0", images=_images()
        )
        rendered = render_release_manifest(manifest)
        assert json.loads(rendered) == _manifest_payload()
        path = tmp_path / "release.json"
        path.write_text(rendered, encoding="utf-8")
        loaded = load_release_manifest(path)
        assert loaded == manifest
        assert loaded.digest("portal") == "sha256:" + "1" * 64
        assert loaded.repository("openvpn") == f"{REGISTRY}/openvpn"

    @pytest.mark.parametrize(
        "payload",
        [
            _manifest_payload(schema="bigrae.gcp-release.v2"),
            _manifest_payload(source_revision="abc123"),
            _manifest_payload(version="has space"),
            _manifest_payload(chart_version="latest"),
            _manifest_payload(images={**_images(), "portal": f"{REGISTRY}/portal:latest"}),
            _manifest_payload(images={k: v for k, v in _images().items() if k != "openvpn"}),
            _manifest_payload(images={**_images(), "extra": f"{REGISTRY}/x@sha256:{'a' * 64}"}),
            _manifest_payload(unexpected=True),
        ],
    )
    def test_rejects_incomplete_or_mutable_manifests(self, payload: dict[str, object]):
        with pytest.raises(InstallationConfigError) as caught:
            parse_release_manifest(json.dumps(payload))
        assert "invalid GCP release manifest" in str(caught.value)

    def test_rejects_non_json_and_unreadable_input(self, tmp_path: Path):
        for text in ("not json", "[]"):
            with pytest.raises(InstallationConfigError):
                parse_release_manifest(text)
        with pytest.raises(InstallationConfigError):
            load_release_manifest(tmp_path / "missing.json")


class TestReleaseManifestCommand:
    def test_writes_validated_manifest_to_stdout_only(self, capsys: pytest.CaptureFixture[str]):
        argv = ["gcp-release-manifest", "--version", "1.4.0", "--source-revision", REVISION, "--chart-version", "0.2.0"]
        for name, identity in _images().items():
            argv += ["--image", f"{name}={identity}"]
        assert main(argv) == 0
        assert parse_release_manifest(capsys.readouterr().out).images == _images()
        # The command opens no caller-named file; a shell redirect selects the destination.
        with pytest.raises(SystemExit):
            main([*argv, "--output", "release.json"])

    def test_pins_base_images_and_embeds_the_authorization_model(self, monkeypatch, capsys):
        """#2679: the release carries its install inputs."""
        argv = ["gcp-release-manifest", "--version", "1.4.0", "--source-revision", REVISION, "--chart-version", "0.2.0"]
        for name, identity in _images().items():
            argv += ["--image", f"{name}={identity}"]
        base = {role: f"ghcr.io/acme/shifter-gce-{role}@sha256:" + "c" * 64 for role in ("kali", "ubuntu", "dc")}
        for role, identity in base.items():
            argv += ["--base-image", f"{role}={identity}"]
        model = json.dumps({"schema_version": "1.1", "type_definitions": [{"type": "principal"}]})
        monkeypatch.setattr("sys.stdin", io.StringIO(model))
        assert main([*argv, "--authorization-model-stdin"]) == 0
        manifest = parse_release_manifest(capsys.readouterr().out)
        assert manifest.base_images == base and manifest.authorization_model["schema_version"] == "1.1"
        monkeypatch.setattr("sys.stdin", io.StringIO(model))
        assert main([*argv, "--base-image", "kali=x", "--authorization-model-stdin"]) == 1
        assert "repeated --base-image" in capsys.readouterr().err
        for text in ("not json", "[]"):
            monkeypatch.setattr("sys.stdin", io.StringIO(text))
            assert main([*argv, "--authorization-model-stdin"]) == 1
        # The command opens no caller-named model file.
        with pytest.raises(SystemExit):
            main([*argv, "--authorization-model-file", "model.json"])

    def test_rejects_malformed_and_duplicate_images(self, capsys: pytest.CaptureFixture[str]):
        base = ["gcp-release-manifest", "--version", "1", "--source-revision", REVISION, "--chart-version", "0.2.0"]
        assert main([*base, "--image", "portal"]) == 1
        assert "--image: must be NAME=REPOSITORY@sha256:DIGEST" in capsys.readouterr().err
        portal = _images()["portal"]
        assert main([*base, "--image", f"portal={portal}", "--image", f"portal={portal}"]) == 1
        assert "release.images.portal: is named more than once" in capsys.readouterr().err
        assert main([*base, "--image", f"portal={portal}"]) == 1
        captured = capsys.readouterr()
        assert "release.images" in captured.err
        assert captured.out == ""
