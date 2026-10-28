"""The portal must reach Guacamole's in-cluster token API under default deny."""

from tests.platform.test_gcp_job_launcher_manifests import _load_helm_documents


def _policy(documents: list[dict], name: str) -> dict:
    return next(
        document
        for document in documents
        if document.get("kind") == "NetworkPolicy" and document["metadata"]["name"] == name
    )


def test_portal_can_reach_guacamole_token_api_under_default_deny() -> None:
    """Both halves of the pod-to-pod path must allow only portal to client:8080."""
    documents = _load_helm_documents()
    ingress = _policy(documents, "allow-portal-to-guacamole-client")["spec"]
    assert ingress == {
        "podSelector": {"matchLabels": {"app.kubernetes.io/component": "guacamole-client"}},
        "policyTypes": ["Ingress"],
        "ingress": [
            {
                "from": [{"podSelector": {"matchLabels": {"app.kubernetes.io/component": "portal"}}}],
                "ports": [{"protocol": "TCP", "port": 8080}],
            }
        ],
    }

    egress = _policy(documents, "allow-portal-egress-to-guacamole-client")["spec"]
    assert egress == {
        "podSelector": {"matchLabels": {"app.kubernetes.io/component": "portal"}},
        "policyTypes": ["Egress"],
        "egress": [
            {
                "to": [{"podSelector": {"matchLabels": {"app.kubernetes.io/component": "guacamole-client"}}}],
                "ports": [{"protocol": "TCP", "port": 8080}],
            }
        ],
    }
