"""Validate operator OpenFGA bundles and project their Kubernetes objects.

Both providers deliver the same operator-provisioned policy bundles: runtime and
admin each hold ``server-preshared-keys``, ``api-token``, ``tls.crt`` and
``tls.key``; the CA bundle holds only ``ca.crt``. The provider fetches them from
its own secret store; this module checks their separation and builds the named
Kubernetes Secrets and the public CA ConfigMap. It never logs a value.
"""

from __future__ import annotations

import base64
from collections.abc import Mapping

BUNDLE_KEYS = frozenset({"server-preshared-keys", "api-token", "tls.crt", "tls.key"})
NAMESPACE = "shifter-platform"


def _bundle_keys(bundle: object) -> set[str]:
    if (
        not isinstance(bundle, dict)
        or set(bundle) != BUNDLE_KEYS
        or not all(isinstance(value, str) and value and "\0" not in value for value in bundle.values())
    ):
        raise ValueError("Policy bundle must contain only the native credential and TLS fields")
    keys = {key.strip() for key in bundle["server-preshared-keys"].split(",")}
    if not keys or any(len(key) < 32 for key in keys):
        raise ValueError("Policy authentication keys must be at least 32 characters")
    return keys


def validate_policy_bundles(runtime: object, admin: object, ca: object) -> None:
    """Require separate runtime/admin keys and TLS keys and a public-only CA bundle."""
    runtime_keys, admin_keys = _bundle_keys(runtime), _bundle_keys(admin)
    assert isinstance(runtime, dict) and isinstance(admin, dict)
    if runtime_keys & admin_keys or runtime["tls.key"] == admin["tls.key"]:
        raise ValueError("Runtime and administration require separate authentication and TLS keys")
    if runtime["api-token"] not in runtime_keys or admin["api-token"] not in admin_keys:
        raise ValueError("Policy client token must match its own native server authentication keys")
    if not isinstance(ca, dict) or set(ca) != {"ca.crt"} or not isinstance(ca["ca.crt"], str) or not ca["ca.crt"]:
        raise ValueError("Policy CA publication accepts only the public CA certificate")


def policy_documents(
    runtime: Mapping[str, str], admin: Mapping[str, str], ca: Mapping[str, str]
) -> list[dict[str, object]]:
    """Return the named policy Secrets and CA ConfigMap after validation.

    The runtime bundle's ``api-token`` maps to the chart's ``runtime-preshared-key``.
    """
    validate_policy_bundles(dict(runtime), dict(admin), dict(ca))
    labels = {"app.kubernetes.io/part-of": "shifter"}
    documents: list[dict[str, object]] = []
    for name, bundle in (("openfga-runtime", runtime), ("openfga-admin", admin)):
        documents.append(
            {
                "apiVersion": "v1",
                "kind": "Secret",
                "metadata": {"name": name, "namespace": NAMESPACE, "labels": labels},
                "type": "Opaque",
                "data": {
                    (
                        "runtime-preshared-key" if name == "openfga-runtime" and key == "api-token" else key
                    ): base64.b64encode(value.encode()).decode()
                    for key, value in bundle.items()
                },
            }
        )
    documents.append(
        {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": "openfga-ca", "namespace": NAMESPACE, "labels": labels},
            "data": dict(ca),
        }
    )
    return documents
