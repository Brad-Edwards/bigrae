"""Behavior tests for gcp_base_images: release guest-image verification and import (#2309, #2679, #2668).

Covers newest-publication discovery, verification of every release guest before any
change, the artifact contract, the provenance gate at the build revision and protected
branch, mirror transport, import and reuse, transfer bucket cleanup, and the generated
GCP_RANGE_*_IMAGE references. The run_cmd boundary
is mocked (ADR-019); one fake runner simulates the oras/gcloud side effects and one
fake OCI reader serves digest-verified manifests.
"""

import json
import os
import subprocess
from pathlib import Path

import pytest

import gcp_base_images as gbi
import gcp_provenance

DIGEST = "sha256:" + "a" * 64
REVISION = "c" * 40
# ADR-069: a synthetic release registry and mirror.
REGISTRY = "ghcr.io/example-org/example-repo"
MIRROR = "us-central1-docker.pkg.dev/example-mirror/bigrae"
_ROLE_ENV = {"kali": "GCP_RANGE_KALI_IMAGE", "ubuntu": "GCP_RANGE_LINUX_IMAGE", "dc": "GCP_RANGE_DC_IMAGE"}


def _completed(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(args=["x"], returncode=returncode, stdout=stdout, stderr=stderr)


def _manifest(role="kali", revision=REVISION):
    return {
        "artifactType": gbi.GCE_IMAGE_ARTIFACT_TYPE,
        "layers": [{"mediaType": gbi.GCE_IMAGE_MEDIA_TYPE, "digest": "sha256:" + "b" * 64}],
        "annotations": {
            "com.shifter.image.role": role,
            "org.opencontainers.image.revision": revision,
        },
    }


def _pins(registry=REGISTRY):
    return {role: f"{registry}/gce-{role}@{DIGEST}" for role in gbi.BASE_IMAGE_ROLES}


def _owned(role):
    return f"oci://{REGISTRY}/gce-{role}@{DIGEST}"


class FakeOci:
    """Serves one manifest per repository and records every read."""

    def __init__(self):
        self.reads: list[str] = []
        self.manifests: dict[str, dict] = {}

    def manifest(self, image):
        self.reads.append(f"{image.host}/{image.name}@{image.digest}")
        role = image.name.rsplit("gce-", 1)[1]
        return self.manifests.get(role, _manifest(role=role))


class FakeRunCmd:
    """Records run_cmd calls and simulates the oras/gcloud side effects.

    ``existing`` maps a GCE image name to the description that ``images describe``
    returns for the reuse check. A missing name means that the image does not exist.
    """

    def __init__(self):
        self.calls: list[list[str]] = []
        self.status = "READY"
        self.existing: dict[str, str] = {}
        self.existing_role: str | None = None
        self.existing_guest_os_features: list[str] | None = None
        self.fail_substr: str | None = None
        self.fail_cleanup = False

    def own(self, *roles):
        for role in roles:
            self.existing[f"shifter-{role}-" + "a" * 12] = _owned(role)

    def _guest_features(self, inferred_role: str) -> list[str]:
        if self.existing_guest_os_features is not None:
            return self.existing_guest_os_features
        features = ["UEFI_COMPATIBLE"]
        if inferred_role == "dc":
            features.append("WINDOWS")
        return features

    def _describe(self, cmd: list[str]):
        image_name = cmd[4]
        if image_name not in self.existing:
            return _completed(returncode=1, stderr="not found")
        inferred_role = image_name.removeprefix("shifter-").rsplit("-", 1)[0]
        return _completed(
            stdout=json.dumps(
                {
                    "description": self.existing[image_name],
                    "labels": {
                        "shifter-base-role": self.existing_role or inferred_role,
                        "shifter-base-digest": "a" * 12,
                    },
                    "status": self.status,
                    "guestOsFeatures": [{"type": feature} for feature in self._guest_features(inferred_role)],
                }
            )
        )

    def __call__(self, cmd, dry_run=False, check=True, capture=False, profile=None):
        cmd = [str(c) for c in cmd]
        self.calls.append(cmd)
        line = " ".join(cmd)
        if self.fail_substr and self.fail_substr in line:
            if check:
                raise SystemExit(1)
            return _completed(returncode=1, stderr="simulated failure")
        if self.fail_cleanup and cmd[:3] == ["gcloud", "storage", "rm"] and "--recursive" in cmd:
            return _completed(returncode=1, stderr="simulated cleanup failure")
        if dry_run:
            return None
        if cmd[:3] == ["oras", "blob", "fetch"]:
            Path(cmd[cmd.index("--output") + 1]).write_bytes(b"rawdisk")
            return _completed()
        if cmd[:4] == ["gcloud", "compute", "images", "describe"]:
            if "--format=json(description,labels,status,guestOsFeatures)" in cmd:
                return self._describe(cmd)
            if "--format=value(status)" in cmd:
                return _completed(stdout=self.status)
        if cmd[:4] == ["gcloud", "compute", "images", "delete"]:
            self.existing.pop(cmd[4], None)
            self.status = "READY"
        return _completed()

    def ran(self, prefix):
        return [c for c in self.calls if c[: len(prefix)] == prefix]


@pytest.fixture
def fake_run(monkeypatch):
    fake = FakeRunCmd()
    monkeypatch.setattr(gbi, "run_cmd", fake)
    return fake


@pytest.fixture
def fake_oci():
    return FakeOci()


@pytest.fixture
def provenance(monkeypatch):
    """Record each provenance check; a role in ``refuse`` fails verification."""
    state = {"checked": [], "refuse": set()}

    def verify(identity, trust, *, oci, verifier):
        state["checked"].append((identity, trust.repository_id, trust.owner_id, trust.source_revision))
        state["refs"] = trust.source_refs
        if any(f"gce-{role}@" in identity for role in state["refuse"]):
            raise gcp_provenance.ProvenanceError("no attestation satisfies the release trust policy")

    monkeypatch.setattr(gcp_provenance, "verify_image_provenance", verify)
    return state


def _resolve(fake_oci, pins=None, release_source=""):
    return gbi.resolve_selection(
        _pins() if pins is None else pins,
        repository_id="123",
        owner_id="456",
        oci=fake_oci,
        verifier=object(),
        release_source=release_source,
    )


class TestPureHelpers:
    def test_transport_identity_keeps_the_digest_and_moves_only_the_location(self):
        canonical = f"{REGISTRY}/gce-dc@{DIGEST}"
        assert gbi.transport_identity("dc", canonical) == canonical
        assert gbi.transport_identity("dc", canonical, MIRROR) == f"{MIRROR}/gce-dc@{DIGEST}"
        for bad_source in ("example-org", "https://registry/x", "registry/x/"):
            with pytest.raises(gbi.BaseImageError):
                gbi.transport_identity("dc", canonical, bad_source)
        with pytest.raises(gbi.BaseImageError):
            gbi.transport_identity("windows", canonical)
        for bad_identity in (
            f"{REGISTRY}/gce-dc:latest",
            f"{REGISTRY}/shifter-gce-dc@{DIGEST}",
            f"{REGISTRY}/gce-kali@{DIGEST}",
        ):
            with pytest.raises(gbi.BaseImageError):
                gbi.transport_identity("dc", bad_identity)

    def test_package_names_carry_the_repository_of_the_app(self):
        assert gbi.package_for_role("kali", REGISTRY) == f"{REGISTRY}/gce-kali"
        with pytest.raises(gbi.BaseImageError):
            gbi.package_for_role("windows", REGISTRY)

    def test_newest_publication_orders_by_run_number_then_attempt(self):
        tags = ["run-9.1", "run-10.1", "run-10.2", "latest", "gce-999999", "run-2.7"]
        assert gbi._newest_publication_tag(tags) == "run-10.2"
        assert gbi._newest_publication_tag(["latest"]) is None

    def test_validate_manifest_returns_the_disk_layer_and_build_revision(self):
        layer, revision = gbi.validate_artifact_manifest(_manifest(role="ubuntu"), role="ubuntu")
        assert (layer, revision) == ("sha256:" + "b" * 64, REVISION)

    @pytest.mark.parametrize(
        "mutate",
        [
            lambda m: m.update({"artifactType": "application/vnd.oci.image.manifest.v1+json"}),
            lambda m: m.update({"layers": []}),
            lambda m: m.update({"layers": [{"mediaType": "application/octet-stream"}]}),
            lambda m: m["annotations"].update({"com.shifter.image.role": "windows"}),
            lambda m: m["annotations"].update({"org.opencontainers.image.revision": "short"}),
            lambda m: m["annotations"].pop("org.opencontainers.image.revision"),
        ],
    )
    def test_validate_manifest_rejects_incompatible(self, mutate):
        manifest = _manifest(role="kali")
        mutate(manifest)
        with pytest.raises(gbi.BaseImageError):
            gbi.validate_artifact_manifest(manifest, role="kali")

    def test_validate_manifest_rejects_non_object(self):
        with pytest.raises(gbi.BaseImageError):
            gbi.validate_artifact_manifest(["not", "a", "dict"], role="kali")

    def test_range_images_from_imports_needs_the_complete_set(self):
        complete = {
            "GCP_RANGE_LINUX_IMAGE": "projects/p/global/images/l",
            "GCP_RANGE_KALI_IMAGE": "projects/p/global/images/k",
            "GCP_RANGE_DC_IMAGE": "projects/p/global/images/d",
        }
        assert gbi.range_images_from_imports(complete) == {
            "linux": "projects/p/global/images/l",
            "kali": "projects/p/global/images/k",
            "dc": "projects/p/global/images/d",
        }
        with pytest.raises(gbi.BaseImageError):
            gbi.range_images_from_imports({"GCP_RANGE_LINUX_IMAGE": "x"})

    def test_image_name_is_digest_derived(self):
        assert gbi.image_name_for("kali", "abc123def456") == "shifter-kali-abc123def456"

    def test_render_range_image_env_maps_roles(self):
        imported = {
            "ubuntu": gbi.ImportedImage("ubuntu", "n", "projects/p/global/images/n", DIGEST, reused=False),
            "kali": gbi.ImportedImage("kali", "k", "projects/p/global/images/k", DIGEST, reused=True),
            "dc": gbi.ImportedImage("dc", "d", "projects/p/global/images/d", DIGEST, reused=False),
        }
        assert gbi.render_range_image_env(imported) == {
            "GCP_RANGE_LINUX_IMAGE": "projects/p/global/images/n",
            "GCP_RANGE_KALI_IMAGE": "projects/p/global/images/k",
            "GCP_RANGE_DC_IMAGE": "projects/p/global/images/d",
        }


class TestNewestPublications:
    def test_takes_the_newest_tag_of_each_role_and_its_digest(self, fake_run, monkeypatch):
        def oras(cmd, dry_run=False, check=True, capture=False, profile=None):
            fake_run.calls.append(cmd)
            if cmd[:3] == ["oras", "repo", "tags"]:
                return _completed(stdout="run-3.1\nrun-12.1\nrun-12.2\n")
            return _completed(stdout=json.dumps({"digest": DIGEST}))

        monkeypatch.setattr(gbi, "run_cmd", oras)
        assert gbi.newest_publications(REGISTRY) == _pins()
        fetches = [c[-1] for c in fake_run.calls if c[:3] == ["oras", "manifest", "fetch"]]
        assert fetches == [f"{REGISTRY}/gce-{role}:run-12.2" for role in gbi.BASE_IMAGE_ROLES]

    def test_a_role_without_a_publication_stops_the_release(self, monkeypatch):
        monkeypatch.setattr(gbi, "run_cmd", lambda *a, **k: _completed(stdout="latest\n"))
        with pytest.raises(gbi.BaseImageError, match="publication tag"):
            gbi.newest_publications(REGISTRY)


class TestResolveSelection:
    def test_verifies_every_role_at_its_build_revision_from_a_protected_branch(self, fake_oci, provenance):
        artifacts = _resolve(fake_oci)
        assert [artifact.role for artifact in artifacts] == list(gbi.BASE_IMAGE_ROLES)
        kali = artifacts[0]
        assert kali.canonical == kali.transport == f"{REGISTRY}/gce-kali@{DIGEST}"
        assert kali.disk_digest == "sha256:" + "b" * 64
        assert kali.pinned_reference == _owned("kali")
        assert provenance["checked"] == [
            (f"{REGISTRY}/gce-{role}@{DIGEST}", "123", "456", REVISION) for role in gbi.BASE_IMAGE_ROLES
        ]
        assert provenance["refs"] == ("refs/heads/dev", "refs/heads/main")

    def test_a_mirror_changes_the_transport_but_not_the_canonical_reference(self, fake_oci, provenance):
        artifacts = _resolve(fake_oci, release_source=MIRROR)
        assert artifacts[0].transport == f"{MIRROR}/gce-kali@{DIGEST}"
        assert artifacts[0].pinned_reference == _owned("kali")
        assert all(read.startswith(MIRROR) for read in fake_oci.reads)
        assert all(identity.startswith(MIRROR) for identity, *_ in provenance["checked"])

    @pytest.mark.parametrize(
        ("pins", "message"),
        [
            ({}, "names no guest images"),
            ({"kali": f"{REGISTRY}/gce-kali@{DIGEST}"}, "exactly the guest images"),
        ],
    )
    def test_refuses_an_empty_or_partial_selection(self, fake_oci, provenance, pins, message):
        with pytest.raises(gbi.BaseImageError, match=message):
            _resolve(fake_oci, pins=pins)
        assert not fake_oci.reads

    def test_an_unsigned_or_foreign_guest_stops_the_whole_selection(self, fake_oci, provenance):
        provenance["refuse"].add("ubuntu")
        with pytest.raises(gbi.BaseImageError, match="ubuntu failed verification"):
            _resolve(fake_oci)

    def test_provenance_is_checked_at_the_revision_that_the_artifact_names(self, fake_oci, provenance):
        fake_oci.manifests["dc"] = _manifest(role="dc", revision="d" * 40)
        artifacts = _resolve(fake_oci)
        assert artifacts[2].revision == "d" * 40
        assert provenance["checked"][2][3] == "d" * 40


class TestImportBaseImage:
    def _artifact(self, role="kali", transport_registry=REGISTRY):
        return gbi.ResolvedArtifact(
            role=role,
            canonical=f"{REGISTRY}/gce-{role}@{DIGEST}",
            transport=f"{transport_registry}/gce-{role}@{DIGEST}",
            disk_digest="sha256:" + "b" * 64,
            revision=REVISION,
        )

    def test_import_creates_native_image_and_cleans_staging(self, fake_run):
        imported = gbi.import_base_image(self._artifact("kali"), project="proj", staging_bucket="stage")
        assert imported.reused is False
        assert imported.image_name == "shifter-kali-" + "a" * 12
        assert imported.image_ref == "projects/proj/global/images/shifter-kali-" + "a" * 12
        create = next(c for c in fake_run.calls if c[:4] == ["gcloud", "compute", "images", "create"])
        assert "gs://stage/base-images/shifter-kali-aaaaaaaaaaaa.tar.gz" in create
        assert "shifter-base-role=kali,shifter-base-digest=" + "a" * 12 in create
        assert "--family" in create and "shifter-kali" in create
        assert create[create.index("--guest-os-features") + 1] == "UEFI_COMPATIBLE"
        # The canonical OCI reference owns the image so reuse can verify it.
        assert _owned("kali") in create
        assert fake_run.ran(["gcloud", "storage", "rm"])
        fetch = next(c for c in fake_run.calls if c[:3] == ["oras", "blob", "fetch"])
        assert fetch[-1] == f"{REGISTRY}/gce-kali@sha256:" + "b" * 64
        assert Path(fetch[fetch.index("--output") + 1]).name == "kali-aaaaaaaaaaaa.tar.gz"

    def test_import_reuses_matching_existing_image(self, fake_run):
        fake_run.own("ubuntu")
        imported = gbi.import_base_image(self._artifact("ubuntu"), project="proj", staging_bucket="stage")
        assert imported.reused is True
        assert not fake_run.ran(["gcloud", "compute", "images", "create"])
        assert not fake_run.ran(["oras", "blob", "fetch"])

    def test_import_from_a_mirror_reuses_the_image_of_the_canonical_reference(self, fake_run):
        fake_run.own("ubuntu")
        artifact = self._artifact("ubuntu", transport_registry=MIRROR)
        assert gbi.import_base_image(artifact, project="proj", staging_bucket="stage").reused is True
        assert not fake_run.ran(["oras", "blob", "fetch"])

    def test_import_from_a_mirror_fetches_the_disk_from_the_mirror(self, fake_run):
        artifact = self._artifact("kali", transport_registry=MIRROR)
        gbi.import_base_image(artifact, project="proj", staging_bucket="stage")
        fetch = next(c for c in fake_run.calls if c[:3] == ["oras", "blob", "fetch"])
        assert fetch[-1] == f"{MIRROR}/gce-kali@sha256:" + "b" * 64
        create = next(c for c in fake_run.calls if c[:4] == ["gcloud", "compute", "images", "create"])
        assert _owned("kali") in create

    def test_import_replaces_owned_ready_image_missing_uefi(self, fake_run):
        fake_run.own("ubuntu")
        fake_run.existing_guest_os_features = []
        imported = gbi.import_base_image(self._artifact("ubuntu"), project="proj", staging_bucket="stage")
        assert imported.reused is False
        assert fake_run.ran(["gcloud", "compute", "images", "delete"])
        create = next(c for c in fake_run.calls if c[:4] == ["gcloud", "compute", "images", "create"])
        assert create[create.index("--guest-os-features") + 1] == "UEFI_COMPATIBLE"

    def test_import_reconciles_owned_failed_status(self, fake_run):
        fake_run.own("ubuntu")
        fake_run.status = "FAILED"
        imported = gbi.import_base_image(self._artifact("ubuntu"), project="proj", staging_bucket="stage")
        assert imported.reused is False
        assert fake_run.ran(["gcloud", "compute", "images", "delete"])
        assert fake_run.ran(["gcloud", "compute", "images", "create"])

    def test_import_does_not_delete_owned_image_while_creation_is_pending(self, fake_run):
        fake_run.own("ubuntu")
        fake_run.status = "PENDING"
        with pytest.raises(gbi.BaseImageError, match="not READY"):
            gbi.import_base_image(self._artifact("ubuntu"), project="proj", staging_bucket="stage")
        assert not fake_run.ran(["gcloud", "compute", "images", "delete"])

    @pytest.mark.parametrize(
        ("description", "role_label"),
        [
            (f"oci://ghcr.io/another-owner/another-package@{DIGEST}", None),
            (_owned("ubuntu"), "kali"),
            (f"oci://{REGISTRY}/gce-ubuntu@sha256:" + "e" * 64, None),
        ],
    )
    def test_import_refuses_an_image_that_it_did_not_create(self, fake_run, description, role_label):
        fake_run.existing["shifter-ubuntu-" + "a" * 12] = description
        fake_run.existing_role = role_label
        with pytest.raises(gbi.BaseImageError, match="refusing to reuse"):
            gbi.import_base_image(self._artifact("ubuntu"), project="proj", staging_bucket="stage")
        assert not fake_run.ran(["gcloud", "compute", "images", "create"])

    def test_import_dc_marks_windows_guest_os_feature(self, fake_run):
        gbi.import_base_image(self._artifact("dc"), project="proj", staging_bucket="stage")
        create = next(c for c in fake_run.calls if c[:4] == ["gcloud", "compute", "images", "create"])
        assert "UEFI_COMPATIBLE,WINDOWS" in create

    def test_import_cleans_staging_even_when_create_fails(self, fake_run):
        fake_run.fail_substr = "images create"
        with pytest.raises(SystemExit):
            gbi.import_base_image(self._artifact("kali"), project="proj", staging_bucket="stage")
        assert fake_run.ran(["gcloud", "storage", "rm"])

    def test_import_fails_when_image_not_ready(self, fake_run):
        fake_run.status = "PENDING"
        with pytest.raises(gbi.BaseImageError):
            gbi.import_base_image(self._artifact("kali"), project="proj", staging_bucket="stage")


class TestImportSelection:
    @pytest.fixture
    def artifacts(self, fake_oci, provenance):
        return _resolve(fake_oci)

    def test_imports_all_roles_and_renders_env(self, fake_run, artifacts):
        imported = gbi.import_selection(artifacts, project="proj")
        assert set(imported) == set(gbi.BASE_IMAGE_ROLES)
        env = gbi.render_range_image_env(imported)
        for role in gbi.BASE_IMAGE_ROLES:
            assert env[_ROLE_ENV[role]] == f"projects/proj/global/images/shifter-{role}-" + "a" * 12

    def test_an_unchanged_selection_creates_no_bucket_and_imports_nothing(self, fake_run, artifacts):
        fake_run.own(*gbi.BASE_IMAGE_ROLES)
        imported = gbi.import_selection(artifacts, project="proj")
        assert all(image.reused for image in imported.values())
        assert not fake_run.ran(["gcloud", "storage", "buckets", "create"])
        assert not fake_run.ran(["oras", "blob", "fetch"])
        assert not fake_run.ran(["gcloud", "compute", "images", "create"])

    def test_one_changed_role_imports_only_that_role(self, fake_run, artifacts):
        fake_run.own("kali", "dc")
        imported = gbi.import_selection(artifacts, project="proj")
        assert [role for role, image in imported.items() if not image.reused] == ["ubuntu"]
        assert len(fake_run.ran(["gcloud", "compute", "images", "create"])) == 1

    def test_dry_run_transfers_nothing(self, fake_run, artifacts):
        gbi.import_selection(artifacts, project="proj", dry_run=True)
        assert not fake_run.ran(["oras", "blob", "fetch"])
        assert not fake_run.ran(["gcloud", "storage", "buckets", "create"])
        assert not fake_run.ran(["gcloud", "compute", "images", "create"])
        assert not any("packer" in " ".join(call) or "workflow" in " ".join(call) for call in fake_run.calls)

    def test_ephemeral_bucket_is_private_and_removed_after_success(self, fake_run, artifacts, monkeypatch):
        monkeypatch.setattr(gbi.uuid, "uuid4", lambda: type("Nonce", (), {"hex": "b" * 32})())
        gbi.import_selection(artifacts, project="proj", region="us-central1")
        create = next(c for c in fake_run.calls if c[:4] == ["gcloud", "storage", "buckets", "create"])
        assert create[4] == "gs://shifter-base-import-proj-bbbbbbbbbbbb"
        assert create[create.index("--project") + 1] == "proj"
        assert create[create.index("--location") + 1] == "us-central1"
        assert "--uniform-bucket-level-access" in create
        assert "--public-access-prevention" in create
        assert "--soft-delete-duration=0" in create
        cleanup = next(c for c in fake_run.calls if c[:3] == ["gcloud", "storage", "rm"] and "--recursive" in c)
        assert "gs://shifter-base-import-proj-bbbbbbbbbbbb/" in cleanup

    @pytest.mark.parametrize(
        ("failure", "status", "expected_error"),
        [
            ("oras blob fetch", "READY", SystemExit),
            ("storage cp", "READY", SystemExit),
            ("images create", "READY", SystemExit),
            (None, "PENDING", gbi.BaseImageError),
        ],
    )
    def test_ephemeral_bucket_is_removed_at_each_import_failure_boundary(
        self, fake_run, artifacts, monkeypatch, failure, status, expected_error
    ):
        monkeypatch.setattr(gbi.uuid, "uuid4", lambda: type("Nonce", (), {"hex": "d" * 32})())
        fake_run.fail_substr = failure
        fake_run.status = status
        with pytest.raises(expected_error):
            gbi.import_selection(artifacts, project="proj", region="us-central1")
        assert any(c[:3] == ["gcloud", "storage", "rm"] and "--recursive" in c for c in fake_run.calls)

    def test_mirror_credentials_reach_only_artifact_registry_hosts(self, fake_run, monkeypatch):
        mirror = f"{MIRROR}/gce-dc@{DIGEST}"
        public = f"{REGISTRY}/gce-kali@{DIGEST}"
        with gbi.registry_credentials(mirror, public):
            config = json.loads(Path(os.environ["DOCKER_CONFIG"], "config.json").read_text(encoding="utf-8"))
        assert config == {"credHelpers": {"us-central1-docker.pkg.dev": "gcloud"}}

    def test_ephemeral_bucket_cleanup_failure_fails_command(self, fake_run, artifacts, monkeypatch):
        monkeypatch.setattr(gbi.uuid, "uuid4", lambda: type("Nonce", (), {"hex": "e" * 32})())
        fake_run.fail_cleanup = True
        with pytest.raises(gbi.BaseImageError, match="cleanup"):
            gbi.import_selection(artifacts, project="proj", region="us-central1")
