"""Tests for the GCP release gates: OCI referrers, provenance, scan, and image import (#2571)."""

from __future__ import annotations

import base64
import hashlib
import io
import json
from pathlib import Path
from urllib import error as urllib_error

import pytest
from sigstore.errors import VerificationError

import gcp_provenance
from gcp_evidence import EvidenceRun
from gcp_oci import (
    INDEX_MEDIA_TYPE,
    SIGSTORE_BUNDLE_ARTIFACT_TYPE,
    ImageReference,
    OciClient,
    OciError,
    bearer_parameters,
    is_artifact_registry_host,
)
from gcp_process import ProcessResult
from gcp_provenance import (
    GITHUB_ACTIONS_OIDC_ISSUER,
    SLSA_PROVENANCE_V1,
    ProvenanceError,
    ReleaseTrust,
    check_statement,
    sigstore_policy,
    verify_image_provenance,
    verify_release_provenance,
)
from gcp_release_images import (
    ReleaseImageError,
    deployment_image_roots,
    docker_config,
    import_release_images,
    registry_hosts,
    release_identities,
    release_tag,
)
from gcp_release_scan import ReleaseScanError, enforce_scan, run_release_scan, scan_release, trivy_argv
from tests.gcp_test_support import REVISION, FakeRunner, release_images, sample_gcp_outputs

HOST = "us-central1-docker.pkg.dev"
NAME = "acme-shifter/shifter-acme-portal/portal"
DIGEST = "sha256:" + "a" * 64
TRUST = ReleaseTrust(repository_id="1234", owner_id="5678", source_revision=REVISION)


def _digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


class _Response(io.BytesIO):
    def __init__(self, body: bytes, status: int = 200):
        super().__init__(body)
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeRegistry:
    """Serve OCI referrers, manifests, and blobs for one repository."""

    def __init__(self, *, referrers_api: bool = True):
        self.bundle = b'{"bundle": "payload"}'
        self.manifest = json.dumps({"layers": [{"digest": _digest(self.bundle)}]}).encode()
        index = {
            "manifests": [
                {"artifactType": SIGSTORE_BUNDLE_ARTIFACT_TYPE, "digest": _digest(self.manifest)},
                {"artifactType": "application/spdx+json", "digest": "sha256:" + "b" * 64},
            ]
        }
        self.index = json.dumps(index).encode()
        self.referrers_api = referrers_api
        self.requests: list[tuple[str, dict[str, str]]] = []

    def __call__(self, request, timeout):
        url = request.full_url
        self.requests.append((url, dict(request.headers)))
        path = url.split("/v2/", 1)[1]
        if "/referrers/" in path:
            if not self.referrers_api:
                raise urllib_error.HTTPError(url, 404, "nf", None, None)
            return _Response(self.index)
        if path.endswith("/manifests/" + DIGEST.replace(":", "-")):
            return _Response(self.index)
        if path.endswith(_digest(self.manifest)):
            return _Response(self.manifest)
        if path.endswith(_digest(self.bundle)):
            return _Response(self.bundle)
        raise urllib_error.HTTPError(url, 404, "nf", None, None)


class TestOciClient:
    @pytest.mark.parametrize("referrers_api", [True, False])
    def test_reads_bundle_payloads_through_the_api_or_tag_fallback(self, referrers_api: bool):
        registry = FakeRegistry(referrers_api=referrers_api)
        client = OciClient(lambda: "token", opener=registry)
        image = ImageReference.parse(f"{HOST}/{NAME}@{DIGEST}")
        assert client.referrer_payloads(image, SIGSTORE_BUNDLE_ARTIFACT_TYPE) == [registry.bundle]
        authorization = registry.requests[0][1]["Authorization"]
        assert base64.b64decode(authorization.split()[1]).decode() == "oauth2accesstoken:token"
        assert all(url.startswith(f"https://{HOST}/v2/{NAME}/") for url, _ in registry.requests)

    def test_public_registry_gets_an_anonymous_token_and_never_the_google_token(self):
        registry = FakeRegistry()
        tokens: list[str] = []

        def public(request, timeout):
            url = request.full_url
            if url.startswith("https://ghcr.io/token?"):
                tokens.append(url)
                assert "Authorization" not in request.headers
                return _Response(b'{"token": "anon"}')
            if request.headers.get("Authorization") != "Bearer anon":
                challenge = 'Bearer realm="https://ghcr.io/token",service="ghcr.io",scope="repository:acme/portal:pull"'
                raise urllib_error.HTTPError(url, 401, "auth", {"WWW-Authenticate": challenge}, None)
            return registry(request, timeout)

        def google_token():
            raise AssertionError("the Google token must not go to a public registry")

        client = OciClient(google_token, opener=public)
        image = ImageReference.parse(f"ghcr.io/acme/portal@{DIGEST}")
        assert client.referrer_payloads(image, SIGSTORE_BUNDLE_ARTIFACT_TYPE) == [registry.bundle]
        assert len(tokens) == 1
        assert "service=ghcr.io" in tokens[0] and "scope=repository%3Aacme%2Fportal%3Apull" in tokens[0]
        assert all(headers.get("Authorization") == "Bearer anon" for _, headers in registry.requests)

    def test_challenge_parsing_and_google_host_detection(self):
        assert bearer_parameters('realm="https://r/t", scope="repository:a/b:pull,push"') == {
            "realm": "https://r/t",
            "scope": "repository:a/b:pull,push",
        }
        assert bearer_parameters('realm="https://r/t",broken') == {"realm": "https://r/t"}
        assert bearer_parameters('realm="unterminated') == {}
        assert bearer_parameters("") == {}
        assert is_artifact_registry_host(HOST)
        for host in ("ghcr.io", "-docker.pkg.dev", "x.evil-docker.pkg.dev", "us-docker.pkg.dev.evil.io"):
            assert not is_artifact_registry_host(host)

    def test_public_registry_challenges_that_cannot_be_met_fail_closed(self):
        image = ImageReference.parse(f"ghcr.io/acme/portal@{DIGEST}")
        for challenge, token_body in (
            ('Basic realm="x"', b""),
            ('Bearer realm="http://ghcr.io/token"', b""),
            ('Bearer realm="https://ghcr.io/token"', b"{}"),
        ):

            def opener(request, timeout, challenge=challenge, token_body=token_body):
                if request.full_url.startswith("https://ghcr.io/token"):
                    return _Response(token_body)
                raise urllib_error.HTTPError(request.full_url, 401, "auth", {"WWW-Authenticate": challenge}, None)

            with pytest.raises(OciError):
                OciClient(lambda: "t", opener=opener).referrers(image, SIGSTORE_BUNDLE_ARTIFACT_TYPE)

    def test_rejects_bad_references_digests_and_unreachable_registries(self):
        with pytest.raises(OciError):
            ImageReference.parse("portal:latest")
        registry = FakeRegistry()
        registry.bundle_tampered = True
        client = OciClient(lambda: "t", opener=lambda request, timeout: _Response(b"tampered"))
        image = ImageReference.parse(f"{HOST}/{NAME}@{DIGEST}")
        with pytest.raises(OciError, match="does not match"):
            client.blob_bytes(image, "sha256:" + "c" * 64)
        with pytest.raises(OciError):
            client.blob_bytes(image, "md5:x")

        def unreachable(request, timeout):
            raise urllib_error.URLError("down")

        with pytest.raises(OciError, match="unreachable"):
            OciClient(lambda: "t", opener=unreachable).referrers(image, SIGSTORE_BUNDLE_ARTIFACT_TYPE)

    def test_missing_referrers_and_bad_indexes(self):
        image = ImageReference.parse(f"{HOST}/{NAME}@{DIGEST}")

        def not_found(request, timeout):
            raise urllib_error.HTTPError(request.full_url, 404, "nf", None, None)

        assert OciClient(lambda: "t", opener=not_found).referrers(image, SIGSTORE_BUNDLE_ARTIFACT_TYPE) == []
        bad = OciClient(lambda: "t", opener=lambda request, timeout: _Response(b"[]"))
        with pytest.raises(OciError):
            bad.referrers(image, SIGSTORE_BUNDLE_ARTIFACT_TYPE)
        assert INDEX_MEDIA_TYPE.endswith("index.v1+json")


def _statement(digest: str = DIGEST, predicate: str = SLSA_PROVENANCE_V1) -> bytes:
    algorithm, value = digest.split(":")
    return json.dumps({"predicateType": predicate, "subject": [{"name": "x", "digest": {algorithm: value}}]}).encode()


class FakeVerifier:
    def __init__(self, results):
        self.results = list(results)
        self.policies = []

    def verify_dsse(self, bundle, policy):
        self.policies.append(policy)
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class TestProvenance:
    def test_policy_binds_issuer_immutable_ids_and_revision(self):
        policy = sigstore_policy(TRUST)
        children = {type(child).__name__: child._value for child in policy._children}
        assert children == {
            "OIDCIssuerV2": GITHUB_ACTIONS_OIDC_ISSUER,
            "OIDCSourceRepositoryIdentifier": "1234",
            "OIDCSourceRepositoryOwnerIdentifier": "5678",
            "OIDCSourceRepositoryDigest": REVISION,
        }
        with pytest.raises(ProvenanceError):
            ReleaseTrust(repository_id="abc", owner_id="1", source_revision=REVISION)
        with pytest.raises(ProvenanceError):
            ReleaseTrust(repository_id="1", owner_id="1", source_revision="short")

    def test_guest_policy_also_binds_the_protected_build_branch(self):
        """#2668: a guest image verifies only when its build ran from dev or main."""
        trust = ReleaseTrust(
            repository_id="1234",
            owner_id="5678",
            source_revision=REVISION,
            source_refs=("refs/heads/dev", "refs/heads/main"),
        )
        branch_rule = sigstore_policy(trust)._children[-1]
        assert type(branch_rule).__name__ == "AnyOf"
        assert [child._value for child in branch_rule._children] == ["refs/heads/dev", "refs/heads/main"]
        with pytest.raises(ProvenanceError):
            ReleaseTrust(repository_id="1", owner_id="1", source_revision=REVISION, source_refs=("refs/tags/v1",))

    def test_statement_checks(self):
        check_statement("application/vnd.in-toto+json", _statement(), DIGEST)
        for payload_type, payload in (
            ("text/plain", _statement()),
            ("application/vnd.in-toto+json", b"not json"),
            ("application/vnd.in-toto+json", _statement(predicate="https://spdx.dev/Document")),
            ("application/vnd.in-toto+json", _statement(digest="sha256:" + "f" * 64)),
        ):
            with pytest.raises(ProvenanceError):
                check_statement(payload_type, payload, DIGEST)

    def test_verifies_the_first_satisfying_bundle(self, monkeypatch):
        monkeypatch.setattr("sigstore.models.Bundle.from_json", staticmethod(lambda raw: raw))
        registry = FakeRegistry()
        oci = OciClient(lambda: "t", opener=registry)
        verifier = FakeVerifier([("application/vnd.in-toto+json", _statement())])
        verify_image_provenance(f"{HOST}/{NAME}@{DIGEST}", TRUST, oci=oci, verifier=verifier)
        assert len(verifier.policies) == 1

    def test_fails_closed_without_a_satisfying_bundle(self, monkeypatch):
        monkeypatch.setattr("sigstore.models.Bundle.from_json", staticmethod(lambda raw: raw))
        oci = OciClient(lambda: "t", opener=FakeRegistry())
        with pytest.raises(ProvenanceError, match="VerificationError"):
            verify_image_provenance(
                f"{HOST}/{NAME}@{DIGEST}", TRUST, oci=oci, verifier=FakeVerifier([VerificationError("bad signer")])
            )
        with pytest.raises(ProvenanceError, match="SLSA"):
            verify_image_provenance(
                f"{HOST}/{NAME}@{DIGEST}",
                TRUST,
                oci=oci,
                verifier=FakeVerifier([("application/vnd.in-toto+json", _statement(predicate="x"))]),
            )

        def none(request, timeout):
            raise urllib_error.HTTPError(request.full_url, 404, "nf", None, None)

        with pytest.raises(ProvenanceError, match="no attestation is stored"):
            verify_image_provenance(
                f"{HOST}/{NAME}@{DIGEST}", TRUST, oci=OciClient(lambda: "t", opener=none), verifier=FakeVerifier([])
            )

        def broken(request, timeout):
            raise urllib_error.URLError("x")

        with pytest.raises(ProvenanceError, match="cannot read"):
            verify_image_provenance(
                f"{HOST}/{NAME}@{DIGEST}", TRUST, oci=OciClient(lambda: "t", opener=broken), verifier=FakeVerifier([])
            )

    def test_release_verification_covers_every_image(self, monkeypatch):
        seen = []
        monkeypatch.setattr(
            gcp_provenance, "verify_image_provenance", lambda identity, trust, **kw: seen.append(identity)
        )
        verify_release_provenance({"a": "x", "b": "y"}, TRUST, token_provider=lambda: "t", verifier=object())
        assert seen == ["x", "y"]


class TestReleaseScan:
    def test_trivy_argv_never_uses_github(self, tmp_path: Path):
        argv = trivy_argv("trivy", "host/repo@sha256:" + "1" * 64, tmp_path / "r.json", tmp_path / ".trivyignore.yaml")
        assert argv[argv.index("--severity") + 1] == "HIGH,CRITICAL"
        assert argv[argv.index("--exit-code") + 1] == "1"
        assert "--ignore-unfixed" in argv
        assert argv[argv.index("--db-repository") + 1] == "mirror.gcr.io/aquasec/trivy-db"
        assert argv[argv.index("--java-db-repository") + 1] == "mirror.gcr.io/aquasec/trivy-java-db"
        registry_hosts = {part.split("/", 1)[0] for part in argv}
        assert "ghcr.io" not in registry_hosts
        assert not any("github" in part for part in argv)

    def _runner(self, failing: set[str]) -> FakeRunner:
        def trivy(argv, env, stdin):
            Path(argv[argv.index("--output") + 1]).write_text("{}\n")
            name = Path(argv[argv.index("--output") + 1]).name.removesuffix("-trivy.json")
            return ProcessResult(1 if name in failing else 0)

        return FakeRunner().on(["gcloud", "auth", "print-access-token"], ProcessResult(0, "tok\n")).on(["trivy"], trivy)

    def test_passing_scan_stores_evidence_and_verdict(self, tmp_path: Path):
        runner = self._runner(set())
        identities = release_images()
        outcome = run_release_scan(
            runner,
            {"X": "scan"},
            identities=identities,
            evidence=EvidenceRun("acme-shifter-release-evidence", REVISION, "r", 1),
            workdir=tmp_path,
            ignore_file=tmp_path / ".trivyignore.yaml",
        )
        assert outcome.passed
        trivy_calls = runner.find("trivy")
        assert len(trivy_calls) == 5
        assert all(call.env["TRIVY_PASSWORD"] == "tok" and call.env["X"] == "scan" for call in trivy_calls)
        uploads = [call.argv[-1] for call in runner.find("gcloud", "storage", "cp")]
        assert any(url.endswith("/release-manifest.json") for url in uploads)
        assert any(url.endswith("/verdict.json") for url in uploads)
        assert all(f"/release-scans/{REVISION}/r/1/" in url for url in uploads)

    def test_public_images_scan_without_the_google_token(self, tmp_path: Path):
        runner = self._runner(set())
        identities = {**release_images(), "portal": f"ghcr.io/acme/bigrae/portal@{DIGEST}"}
        run_release_scan(
            runner,
            {},
            identities=identities,
            evidence=EvidenceRun("acme-shifter-release-evidence", REVISION, "r", 1),
            workdir=tmp_path,
            ignore_file=tmp_path / ".trivyignore.yaml",
        )
        by_image = {call.argv[-1]: call.env for call in runner.find("trivy")}
        assert "TRIVY_PASSWORD" not in by_image[identities["portal"]]
        assert by_image[identities["guacd"]]["TRIVY_PASSWORD"] == "tok"
        public = self._runner(set())
        run_release_scan(
            public,
            {},
            identities={"portal": identities["portal"]},
            evidence=EvidenceRun("acme-shifter-release-evidence", REVISION, "r", 2),
            workdir=tmp_path / "public",
            ignore_file=tmp_path / ".trivyignore.yaml",
        )
        assert not public.find("gcloud", "auth")

    def test_a_finding_blocks_the_rollout_after_storing_evidence(self, tmp_path: Path):
        runner = self._runner({"guacd"})
        with pytest.raises(ReleaseScanError, match="blocked"):
            run_release_scan(
                runner,
                {},
                identities=release_images(),
                evidence=EvidenceRun("acme-shifter-release-evidence", REVISION, "r", 1),
                workdir=tmp_path,
                ignore_file=tmp_path / ".trivyignore.yaml",
            )
        verdict = json.loads((tmp_path / "release-scan" / "verdict.json").read_text())
        assert verdict["result"] == "failure"

    def test_scan_requires_a_token_and_reports_evidence_failures(self, tmp_path: Path):
        no_token = FakeRunner().on(["gcloud", "auth"], ProcessResult(0, ""))
        with pytest.raises(ReleaseScanError):
            scan_release(
                no_token,
                {},
                identities=release_images(),
                evidence=EvidenceRun("b-evidence", REVISION, "r", 1),
                workdir=tmp_path,
                ignore_file=tmp_path / "i",
            )
        broken = self._runner(set()).on(["gcloud", "storage"], ProcessResult(1))
        broken.responses.insert(0, (["gcloud", "storage"], ProcessResult(1)))
        with pytest.raises(ReleaseScanError, match="could not complete"):
            run_release_scan(
                broken,
                {},
                identities=release_images(),
                evidence=EvidenceRun("b-evidence", REVISION, "r", 1),
                workdir=tmp_path,
                ignore_file=tmp_path / "i",
            )


class TestReleaseImageImport:
    def test_roots_hosts_tags_and_docker_config(self, tmp_path: Path):
        roots = deployment_image_roots(sample_gcp_outputs("acme-shifter"))
        assert roots["provisioner"].endswith("/pulumi-provisioner")
        assert registry_hosts("a.dev/x/y@sha256:1", "b.dev/z") == ["a.dev", "b.dev"]
        assert release_tag(REVISION) == "release-0123456789ab"
        config = json.loads((docker_config(tmp_path, [HOST, "ghcr.io"]) / "config.json").read_text())
        assert config == {"credHelpers": {HOST: "gcloud"}}
        for broken in (
            {},
            {"artifact_registry_image_roots": {"value": []}},
            {"artifact_registry_image_roots": {"value": {}}},
        ):
            with pytest.raises(ReleaseImageError):
                deployment_image_roots(broken)

    def test_copies_release_images_and_keeps_ci_builds(self, tmp_path: Path):
        outputs = sample_gcp_outputs("acme-shifter")
        roots = deployment_image_roots(outputs)
        images = release_images()
        images["guacd"] = f"{roots['guacd']}@sha256:" + "3" * 64

        def resolve(argv, env, stdin):
            root = argv[-1].rsplit(":", 1)[0]
            name = next(key for key, value in roots.items() if value == root)
            return ProcessResult(0, images[name].rsplit("@", 1)[1] + "\n")

        runner = FakeRunner().on(["oras", "resolve"], resolve)
        identities = import_release_images(
            runner, {}, release_images=images, roots=roots, source_revision=REVISION, workdir=tmp_path
        )
        assert identities["guacd"] == images["guacd"]
        assert identities["portal"] == f"{roots['portal']}@sha256:" + "1" * 64
        copies = runner.find("oras", "cp")
        assert len(copies) == 4
        assert all(call.argv[2] == "--recursive" and "DOCKER_CONFIG" in call.env for call in copies)

    def test_release_identities_map_a_mirror_by_name_and_digest(self):
        images = {"portal": "ghcr.io/acme/bigrae/portal@" + DIGEST}
        assert release_identities(images) == images
        assert release_identities(images, "mirror.example:5000/team/bigrae") == {
            "portal": "mirror.example:5000/team/bigrae/portal@" + DIGEST
        }
        for source in ("https://mirror.example/x", "mirror.example", "Mirror.example/x"):
            with pytest.raises(ReleaseImageError, match="registry prefix"):
                release_identities(images, source)
        with pytest.raises(ReleaseImageError, match="exact digest"):
            release_identities({"portal": "ghcr.io/acme/portal:latest"}, "mirror.example/x")

    def test_digest_mismatch_or_copy_failure_stops_the_import(self, tmp_path: Path):
        roots = deployment_image_roots(sample_gcp_outputs("acme-shifter"))
        wrong = FakeRunner().on(["oras", "resolve"], ProcessResult(0, "sha256:" + "f" * 64))
        with pytest.raises(ReleaseImageError, match="different digest"):
            import_release_images(
                wrong, {}, release_images=release_images(), roots=roots, source_revision=REVISION, workdir=tmp_path
            )
        failing = FakeRunner().on(["oras", "cp"], ProcessResult(1, "", "denied"))
        with pytest.raises(ReleaseImageError, match="cannot import"):
            import_release_images(
                failing, {}, release_images=release_images(), roots=roots, source_revision=REVISION, workdir=tmp_path
            )
        with pytest.raises(ReleaseImageError, match="exact digest"):
            import_release_images(
                FakeRunner(),
                {},
                release_images={"portal": "x/y@latest"},
                roots=roots,
                source_revision=REVISION,
                workdir=tmp_path,
            )

    def test_enforce_scan_passes_clean_outcomes(self):
        from gcp_evidence import EvidenceRecord
        from gcp_release_scan import ScanOutcome

        enforce_scan(ScanOutcome(passed=True, failed_images=(), record=EvidenceRecord("p", "l", "s")))
