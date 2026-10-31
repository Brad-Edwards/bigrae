"""Release provenance verification without GitHub (#2571, ADR-037-R6, ADR-070).

The release workflow attests each image with GitHub Actions and stores the Sigstore
bundle as an OCI referrer of the image. This module fetches those bundles from the
image's own registry and verifies them with sigstore-python against the Sigstore
public-good trust root. It never calls the GitHub API.

The verification policy binds:

- the GitHub Actions OIDC issuer;
- the immutable source repository and owner IDs from the deployment trust anchor
  (``settings.release_trust``, ADR-069-R3);
- the source revision of the release manifest;
- for a guest image (#2668), the protected branch that the build ran from.

The verified in-toto statement must use the SLSA provenance v1 predicate and name the
exact image digest as a subject. A missing, unverifiable, or mismatched attestation
stops the deploy. There is no bypass.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from bootstrap_core import info, success
from gcp_oci import SIGSTORE_BUNDLE_ARTIFACT_TYPE, ImageReference, OciClient, OciError

if TYPE_CHECKING:
    from sigstore.models import Bundle
    from sigstore.verify import Verifier
    from sigstore.verify.policy import AllOf, VerificationPolicy

GITHUB_ACTIONS_OIDC_ISSUER = "https://token.actions.githubusercontent.com"
SLSA_PROVENANCE_V1 = "https://slsa.dev/provenance/v1"
IN_TOTO_PAYLOAD_TYPE = "application/vnd.in-toto+json"
_NUMERIC_ID_RE = re.compile(r"^[1-9]\d*$", re.ASCII)
_REVISION_RE = re.compile(r"^[0-9a-f]{40}$")
_BRANCH_REF_RE = re.compile(r"^refs/heads/[A-Za-z0-9][A-Za-z0-9_/-]*$", re.ASCII)


class ProvenanceError(RuntimeError):
    """An image has no attestation that satisfies the release trust policy."""


@dataclass(frozen=True)
class ReleaseTrust:
    """The deployment trust anchor and the release revision that every image must match."""

    repository_id: str
    owner_id: str
    source_revision: str
    # When set, the signing workflow must have run from one of these branches.
    source_refs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not _NUMERIC_ID_RE.fullmatch(self.repository_id) or not _NUMERIC_ID_RE.fullmatch(self.owner_id):
            raise ProvenanceError("release trust requires numeric repository and owner IDs")
        if not _REVISION_RE.fullmatch(self.source_revision):
            raise ProvenanceError("release trust requires a full 40-character source revision")
        if not all(_BRANCH_REF_RE.fullmatch(ref) for ref in self.source_refs):
            raise ProvenanceError("release trust source refs must be branch refs (refs/heads/<branch>)")


class DsseVerifier(Protocol):
    """The part of ``sigstore.verify.Verifier`` that the provenance check uses."""

    def verify_dsse(self, bundle: Bundle, policy: VerificationPolicy) -> tuple[str, bytes]:
        """Verify a DSSE bundle against ``policy`` and return its payload type and payload."""
        ...


def sigstore_policy(trust: ReleaseTrust) -> AllOf:
    """Build the sigstore-python verification policy for ``trust``."""
    from sigstore.verify import policy

    rules: list[VerificationPolicy] = [
        policy.OIDCIssuerV2(GITHUB_ACTIONS_OIDC_ISSUER),
        policy.OIDCSourceRepositoryIdentifier(trust.repository_id),
        policy.OIDCSourceRepositoryOwnerIdentifier(trust.owner_id),
        policy.OIDCSourceRepositoryDigest(trust.source_revision),
    ]
    if trust.source_refs:
        rules.append(policy.AnyOf([policy.OIDCSourceRepositoryRef(ref) for ref in trust.source_refs]))
    return policy.AllOf(rules)


def check_statement(payload_type: str, payload: bytes, digest: str) -> None:
    """Require an SLSA v1 provenance statement whose subjects include ``digest``."""
    if payload_type != IN_TOTO_PAYLOAD_TYPE:
        raise ProvenanceError("attestation payload is not an in-toto statement")
    try:
        statement = json.loads(payload)
    except json.JSONDecodeError:
        raise ProvenanceError("attestation statement is not valid JSON") from None
    if not isinstance(statement, dict) or statement.get("predicateType") != SLSA_PROVENANCE_V1:
        raise ProvenanceError("attestation is not SLSA provenance v1")
    algorithm, _, value = digest.partition(":")
    subjects = statement.get("subject", [])
    if not isinstance(subjects, list) or not any(
        isinstance(subject, dict)
        and isinstance(subject.get("digest"), dict)
        and subject["digest"].get(algorithm) == value
        for subject in subjects
    ):
        raise ProvenanceError("attestation subject does not name the exact image digest")


def default_verifier() -> Verifier:
    """Return the verifier for the Sigstore public-good production instance."""
    from sigstore.verify import Verifier

    return Verifier.production()


def verify_image_provenance(
    identity: str,
    trust: ReleaseTrust,
    *,
    oci: OciClient,
    verifier: DsseVerifier,
) -> None:
    """Verify that at least one stored attestation of ``identity`` satisfies ``trust``."""
    from sigstore.errors import Error as SigstoreError
    from sigstore.models import Bundle

    image = ImageReference.parse(identity)
    try:
        bundles = oci.referrer_payloads(image, SIGSTORE_BUNDLE_ARTIFACT_TYPE)
    except OciError as exc:
        raise ProvenanceError(f"cannot read attestations of {image.name}: {exc}") from None
    if not bundles:
        raise ProvenanceError(f"no attestation is stored with {image.name}@{image.digest[:19]}")
    policy = sigstore_policy(trust)
    reasons: list[str] = []
    for raw in bundles:
        try:
            bundle = Bundle.from_json(raw)
            payload_type, payload = verifier.verify_dsse(bundle, policy)
            check_statement(payload_type, payload, image.digest)
        except ProvenanceError as exc:
            reasons.append(str(exc))
            continue
        except (SigstoreError, ValueError) as exc:
            reasons.append(type(exc).__name__)
            continue
        return
    raise ProvenanceError(
        f"no attestation of {image.name}@{image.digest[:19]} satisfies the release trust policy ("
        + "; ".join(sorted(set(reasons)))
        + ")"
    )


def verify_release_provenance(
    identities: Mapping[str, str],
    trust: ReleaseTrust,
    *,
    token_provider: Callable[[], str],
    oci: OciClient | None = None,
    verifier: DsseVerifier | None = None,
) -> None:
    """Verify every release image before any runtime mutation (ADR-037-R6)."""
    client = oci or OciClient(token_provider)
    selected = verifier or default_verifier()
    for name, identity in identities.items():
        info(f"Verifying release provenance for {name}")
        verify_image_provenance(identity, trust, oci=client, verifier=selected)
    success("Release provenance verified for every image")
