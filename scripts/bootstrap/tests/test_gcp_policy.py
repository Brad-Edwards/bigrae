"""Tests for the generated OpenFGA credentials and the release model publication (#2679)."""

from __future__ import annotations

import json

import pytest
from cryptography import x509
from cryptography.hazmat.primitives.asymmetric import ec

import gcp_policy
from gcp_policy import (
    PolicySetupError,
    ensure_policy_secrets,
    generate_policy_bundles,
    parse_binding,
    publish_policy_model,
    release_model,
    render_publisher,
)
from gcp_process import ProcessResult
from tests.gcp_test_support import FakeRunner

REFERENCES = {
    "runtime": "projects/acme-shifter/secrets/shifter-acme-openfga-runtime",
    "admin": "projects/acme-shifter/secrets/shifter-acme-openfga-admin",
    "ca": "projects/acme-shifter/secrets/shifter-acme-openfga-ca",
}
PORTAL = {
    "kind": "Deployment",
    "metadata": {"name": "portal-web", "namespace": "shifter-platform"},
    "spec": {
        "template": {
            "spec": {
                "containers": [
                    {
                        "name": "portal",
                        "image": "registry.example/portal@sha256:" + "1" * 64,
                        "imagePullPolicy": "IfNotPresent",
                        "securityContext": {"readOnlyRootFilesystem": True},
                    }
                ]
            }
        }
    },
}
MODEL = {"schema_version": "1.1", "type_definitions": [{"type": "principal"}]}


class TestGeneratedCredentials:
    def test_bundles_chain_to_one_ca_and_keep_runtime_and_admin_separate(self):
        bundles = generate_policy_bundles()
        assert set(bundles) == {"runtime", "admin", "ca"}
        assert set(bundles["ca"]) == {"ca.crt"}
        ca = x509.load_pem_x509_certificate(bundles["ca"]["ca.crt"].encode())
        for purpose, server in (("runtime", "openfga"), ("admin", "openfga-admin")):
            bundle = bundles[purpose]
            assert set(bundle) == {"server-preshared-keys", "api-token", "tls.crt", "tls.key"}
            assert bundle["api-token"] == bundle["server-preshared-keys"] and len(bundle["api-token"]) >= 32
            cert = x509.load_pem_x509_certificate(bundle["tls.crt"].encode())
            cert.verify_directly_issued_by(ca)
            names = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
            assert f"{server}.shifter-platform.svc.cluster.local" in names.get_values_for_type(x509.DNSName)
            assert isinstance(cert.public_key(), ec.EllipticCurvePublicKey)
            # RFC 5280 key identifiers link the server certificate to the CA.
            authority = cert.extensions.get_extension_for_class(x509.AuthorityKeyIdentifier).value
            subject = ca.extensions.get_extension_for_class(x509.SubjectKeyIdentifier).value
            assert authority.key_identifier == subject.digest
        assert bundles["runtime"]["api-token"] != bundles["admin"]["api-token"]
        assert bundles["runtime"]["tls.key"] != bundles["admin"]["tls.key"]

    def test_creates_all_three_secrets_through_stdin_only_when_none_exists(self):
        absent = FakeRunner().on(["gcloud", "secrets", "versions", "describe"], ProcessResult(1))
        assert ensure_policy_secrets(absent, REFERENCES) is True
        created = absent.find("gcloud", "secrets", "create")
        assert [call.argv[3] for call in created] == [
            "shifter-acme-openfga-runtime",
            "shifter-acme-openfga-admin",
            "shifter-acme-openfga-ca",
        ]
        for call in created:
            assert "--data-file=-" in call.argv and json.loads(call.input_text)
            assert not any("BEGIN" in part for part in call.argv)
        present = FakeRunner()
        assert ensure_policy_secrets(present, REFERENCES) is False
        assert not present.find("gcloud", "secrets", "create")

    def test_refuses_a_partial_set_and_malformed_references(self):
        def describe(argv, env, stdin):
            return ProcessResult(0 if "shifter-acme-openfga-ca" in argv else 1)

        partial = FakeRunner().on(["gcloud", "secrets", "versions", "describe"], describe)
        with pytest.raises(PolicySetupError, match="Only some OpenFGA secrets exist"):
            ensure_policy_secrets(partial, REFERENCES)
        assert not partial.find("gcloud", "secrets", "create")
        with pytest.raises(PolicySetupError, match="projects/<project>/secrets/<name>"):
            ensure_policy_secrets(FakeRunner(), {"runtime": "secrets/x"})


class TestPublication:
    def test_publisher_runs_the_release_portal_image_in_the_publisher_lane(self):
        config_map, job = render_publisher([PORTAL], name="openfga-publish-x", model=MODEL, script="print(1)")
        assert json.loads(config_map["data"]["model.json"]) == MODEL
        pod = job["spec"]["template"]["spec"]
        assert job["spec"]["template"]["metadata"]["labels"]["app.kubernetes.io/component"] == "openfga-publisher"
        assert pod["automountServiceAccountToken"] is False
        container = pod["containers"][0]
        assert container["image"] == PORTAL["spec"]["template"]["spec"]["containers"][0]["image"]
        assert container["command"] == ["python", "-B", "/publisher/publish.py"]
        token = next(volume for volume in pod["volumes"] if volume["name"] == "admin-token")
        assert token["secret"]["items"] == [{"key": "api-token", "path": "token"}]
        # The non-root portal user reads the 0440 token through the pod group.
        assert token["secret"]["defaultMode"] == 0o440 and pod["securityContext"]["fsGroup"] == 1000
        with pytest.raises(PolicySetupError):
            render_publisher([], name="x", model=MODEL, script="")

    def test_publish_returns_the_binding_and_cleans_up(self):
        def logs(argv, env, stdin):
            return ProcessResult(0, 'noise\nBIGRAE_OPENFGA_BINDING={"model_id": "M1", "store_id": "S1"}\n')

        runner = FakeRunner().on(["kubectl", "logs"], logs)
        assert publish_policy_model(runner, [PORTAL], MODEL) == ("S1", "M1")
        deletes = [call.argv[2] for call in runner.find("kubectl", "delete")]
        assert deletes == ["job", "configmap"]

    def test_binding_and_model_are_required(self):
        for logs in (
            "",
            'BIGRAE_OPENFGA_BINDING={"store_id": "S"}',
            'BIGRAE_OPENFGA_BINDING={"store_id": "", "model_id": ""}',
        ):
            with pytest.raises(PolicySetupError):
                parse_binding(logs)
        assert release_model(MODEL) == MODEL
        with pytest.raises(PolicySetupError, match="no authorization model"):
            release_model(None)
        assert gcp_policy.STORE_NAME == "bigrae-installation"


def test_policy_admin_availability_needs_an_available_replica():
    """#2694: only a serving admin server lets the deploy publish before the probe."""
    from gcp_policy import policy_admin_available

    get = ["kubectl", "get", "deployment", "openfga-admin"]
    assert policy_admin_available(FakeRunner().on(get, ProcessResult(0, "1"))) is True
    for result in (ProcessResult(0, ""), ProcessResult(0, "0"), ProcessResult(1, "", "NotFound")):
        assert policy_admin_available(FakeRunner().on(get, result)) is False
