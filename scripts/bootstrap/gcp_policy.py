"""OpenFGA credentials and model publication for a GitHub-less GCP deploy (#2679).

The current release requires OpenFGA (#2321). A fresh install sets no OpenFGA
inputs, so the deploy does the one-time work itself:

- ``ensure_policy_secrets`` generates the CA, the runtime and admin server TLS
  bundles, and separate preshared keys, and stores them in the three derived
  Secret Manager secrets. It never overwrites an existing secret, and it stops
  when only some of the three exist.
- ``publish_policy_model`` runs a short-lived Job in the ``openfga-publisher``
  network lane. The Job reuses or creates the installation store and reuses or
  writes the release's authorization model, then returns their IDs.

No payload reaches argv or the logs: secret payloads go through stdin, and the
Job reads the admin token and the CA from mounted files.
"""

from __future__ import annotations

import datetime
import json
import secrets
from collections.abc import Mapping, Sequence
from uuid import uuid4

from bootstrap_core import get_repo_root, info, success
from gcp_identity_cutover import _apply, _delete
from gcp_kube import read_job_log
from gcp_process import ProcessRunner

NAMESPACE = "shifter-platform"
STORE_NAME = "bigrae-installation"
RESULT_PREFIX = "BIGRAE_OPENFGA_BINDING="
_SERVERS = ("openfga", "openfga-admin")
_CA_DAYS = 3650
_SERVER_DAYS = 1095


class PolicySetupError(RuntimeError):
    """OpenFGA credentials or the model could not be set up safely."""


def _secret_name(reference: str) -> tuple[str, str]:
    """Split ``projects/<project>/secrets/<name>`` into the project and the name."""
    parts = reference.split("/")
    if len(parts) != 4 or parts[0] != "projects" or parts[2] != "secrets":
        raise PolicySetupError("OpenFGA secret references must be projects/<project>/secrets/<name>")
    return parts[1], parts[3]


def _secret_exists(runner: ProcessRunner, reference: str) -> bool:
    """True when the secret has a latest version."""
    project, name = _secret_name(reference)
    result = runner.run(
        ["gcloud", "secrets", "versions", "describe", "latest", "--secret", name, "--project", project],
        check=False,
    )
    return result.returncode == 0


def _server_names(server: str) -> list[str]:
    """Return the in-cluster DNS names that a server certificate covers."""
    return [server, f"{server}.{NAMESPACE}", f"{server}.{NAMESPACE}.svc", f"{server}.{NAMESPACE}.svc.cluster.local"]


def generate_policy_bundles() -> dict[str, dict[str, str]]:
    """Generate the CA and the separate runtime and admin bundles that the chart reads."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

    now = datetime.datetime.now(datetime.UTC)

    def pem_key(key: ec.EllipticCurvePrivateKey) -> str:
        """Return the unencrypted PKCS #8 PEM of ``key``."""
        return key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
        ).decode()

    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "BigRAE OpenFGA CA")])
    ca_cert = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=_CA_DAYS))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=False,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    bundles: dict[str, dict[str, str]] = {"ca": {"ca.crt": ca_cert.public_bytes(serialization.Encoding.PEM).decode()}}
    for server in _SERVERS:
        key = ec.generate_private_key(ec.SECP256R1())
        names = _server_names(server)
        cert = (
            x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, names[-1])]))
            .issuer_name(ca_name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(minutes=5))
            .not_valid_after(now + datetime.timedelta(days=_SERVER_DAYS))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName(name) for name in names]), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            # RFC 5280 key identifiers; strict TLS clients require them on an issued certificate.
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
            .sign(ca_key, hashes.SHA256())
        )
        token = secrets.token_urlsafe(48)
        bundles["runtime" if server == "openfga" else "admin"] = {
            "server-preshared-keys": token,
            "api-token": token,
            "tls.crt": cert.public_bytes(serialization.Encoding.PEM).decode(),
            "tls.key": pem_key(key),
        }
    return bundles


def ensure_policy_secrets(runner: ProcessRunner, references: Mapping[str, str]) -> bool:
    """Create the three OpenFGA secrets when none exists. Return True when this run created them."""
    present = {purpose: _secret_exists(runner, reference) for purpose, reference in references.items()}
    if all(present.values()):
        return False
    if any(present.values()):
        missing = ", ".join(sorted(references[purpose] for purpose, found in present.items() if not found))
        raise PolicySetupError(
            f"Only some OpenFGA secrets exist; missing: {missing}. Restore them, or delete all three to regenerate."
        )
    info("Generating the OpenFGA CA, server certificates, and preshared keys")
    bundles = generate_policy_bundles()
    for purpose, reference in references.items():
        project, name = _secret_name(reference)
        runner.run(
            [
                "gcloud",
                "secrets",
                "create",
                name,
                "--project",
                project,
                "--replication-policy=automatic",
                "--data-file=-",
            ],
            input_text=json.dumps(bundles[purpose], sort_keys=True),
            sensitive=True,
        )
    success("Stored the generated OpenFGA credentials in Secret Manager")
    return True


def _portal_container(documents: Sequence[Mapping[str, object]]) -> Mapping[str, object]:
    """Return the release portal container, the source of the publisher image."""
    for document in documents:
        metadata = document.get("metadata") or {}
        if (
            document.get("kind") == "Deployment"
            and isinstance(metadata, Mapping)
            and metadata.get("name") == "portal-web"
            and metadata.get("namespace") == NAMESPACE
        ):
            pod = document["spec"]["template"]["spec"]  # type: ignore[index]
            return next(item for item in pod["containers"] if item["name"] == "portal")  # type: ignore[index]
    raise PolicySetupError("Release manifest does not contain the portal-web Deployment")


def render_publisher(
    documents: Sequence[Mapping[str, object]], *, name: str, model: Mapping[str, object], script: str
) -> tuple[dict[str, object], dict[str, object]]:
    """Render the publisher ConfigMap and Job from the exact release portal image."""
    portal = _portal_container(documents)
    labels = {"app.kubernetes.io/part-of": "shifter", "app.kubernetes.io/component": "openfga-publisher"}
    config_map = {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {"name": name, "namespace": NAMESPACE, "labels": labels},
        "data": {"publish.py": script, "model.json": json.dumps(model, sort_keys=True)},
    }
    container: dict[str, object] = {
        "name": "publisher",
        "image": portal["image"],
        "command": ["python", "-B", "/publisher/publish.py"],
        "env": [
            {"name": "OPENFGA_API_URL", "value": f"https://openfga-admin.{NAMESPACE}.svc.cluster.local"},
            {"name": "OPENFGA_STORE_NAME", "value": STORE_NAME},
            {"name": "OPENFGA_TOKEN_FILE", "value": "/run/openfga-admin/token"},
            {"name": "OPENFGA_CA_FILE", "value": "/run/openfga-ca/ca.crt"},
            {"name": "OPENFGA_MODEL_FILE", "value": "/publisher/model.json"},
        ],
        "securityContext": portal.get("securityContext", {}),
        "resources": portal.get("resources", {}),
        "volumeMounts": [
            {"name": "publisher", "mountPath": "/publisher", "readOnly": True},
            {"name": "admin-token", "mountPath": "/run/openfga-admin", "readOnly": True},
            {"name": "ca", "mountPath": "/run/openfga-ca", "readOnly": True},
        ],
    }
    if "imagePullPolicy" in portal:
        container["imagePullPolicy"] = portal["imagePullPolicy"]
    job = {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {"name": name, "namespace": NAMESPACE, "labels": labels},
        "spec": {
            "backoffLimit": 0,
            "activeDeadlineSeconds": 600,
            "template": {
                "metadata": {"labels": labels},
                "spec": {
                    "restartPolicy": "Never",
                    "automountServiceAccountToken": False,
                    # The owner-and-group-only token is readable through the pod group,
                    # as in the cutover and catalog Jobs.
                    "securityContext": {
                        "runAsNonRoot": True,
                        "seccompProfile": {"type": "RuntimeDefault"},
                        "fsGroup": 1000,
                        "fsGroupChangePolicy": "OnRootMismatch",
                    },
                    "containers": [container],
                    "volumes": [
                        {"name": "publisher", "configMap": {"name": name}},
                        {
                            "name": "admin-token",
                            "secret": {
                                "secretName": "openfga-admin",
                                "items": [{"key": "api-token", "path": "token"}],
                                "defaultMode": 0o440,
                            },
                        },
                        {"name": "ca", "configMap": {"name": "openfga-ca"}},
                    ],
                },
            },
        },
    }
    return config_map, job


def parse_binding(logs: str) -> tuple[str, str]:
    """Return the store and model IDs from the publisher's one result line."""
    lines = [line[len(RESULT_PREFIX) :] for line in logs.splitlines() if line.startswith(RESULT_PREFIX)]
    try:
        if len(lines) != 1:
            raise ValueError
        binding = json.loads(lines[0])
        store_id, model_id = str(binding["store_id"]), str(binding["model_id"])
    except (ValueError, KeyError, TypeError):
        raise PolicySetupError("The OpenFGA publisher returned no binding") from None
    if not store_id or not model_id:
        raise PolicySetupError("The OpenFGA publisher returned an empty binding")
    return store_id, model_id


def publish_policy_model(
    runner: ProcessRunner, documents: Sequence[Mapping[str, object]], model: Mapping[str, object]
) -> tuple[str, str]:
    """Publish the release model in a bounded Job and return the store and model IDs."""
    script = (get_repo_root() / "scripts" / "gcp" / "openfga_publish.py").read_text(encoding="utf-8")
    name = f"openfga-publish-{uuid4().hex[:12]}"
    config_map, job = render_publisher(documents, name=name, model=model, script=script)
    info("Publishing the release authorization model to OpenFGA")
    try:
        _apply(runner, config_map)
        _apply(runner, job)
        runner.run(
            ["kubectl", "wait", "--for=condition=complete", f"job/{name}", "-n", NAMESPACE, "--timeout=10m"],
            sensitive=True,
        )
        logs = read_job_log(runner, name, namespace=NAMESPACE)
    finally:
        _delete(runner, "job", name)
        _delete(runner, "configmap", name)
    store_id, model_id = parse_binding(logs)
    success("OpenFGA store and authorization model are published")
    return store_id, model_id


def policy_admin_available(runner: ProcessRunner) -> bool:
    """Return True when the OpenFGA admin server of an existing installation already serves (#2694)."""
    result = runner.run(
        [
            "kubectl",
            "get",
            "deployment",
            "openfga-admin",
            "-n",
            NAMESPACE,
            "-o",
            "jsonpath={.status.availableReplicas}",
        ],
        check=False,
    )
    return result.returncode == 0 and result.stdout.strip() not in ("", "0")


def release_model(manifest_model: object) -> dict[str, object]:
    """Require the release to carry its authorization model."""
    if not isinstance(manifest_model, Mapping) or not manifest_model.get("type_definitions"):
        raise PolicySetupError(
            "This release has no authorization model. Pin settings.openfga.store_id and model_id, "
            "or deploy a release that publishes its model."
        )
    return dict(manifest_model)
