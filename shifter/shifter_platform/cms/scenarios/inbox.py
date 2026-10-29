"""In-box scenario bootstrap seed (#1578, ADR-034).

The packs registered into the tenant by default are *not* loaded through a
privileged code path. They are declared in a manifest and registered through the
SAME :func:`cms.services.register_pack` service an operator uses. This is the
dogfooding requirement of ADR-053/ADR-034: the in-box seed and operator
content share one ingestion path.

The shipped manifest (:data:`SHIPPED_INBOX_MANIFEST`) declares the in-box
seed. The mechanism is exercised by tests; entries are added as
first-party packs are authored.

Bootstrap asks the service for an idempotent retry: an exact immutable identity
is a no-op, while manifest or byte drift is a visible conflict.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import yaml
from django.core.exceptions import ValidationError
from django.core.validators import validate_slug
from django.db import transaction

from cms.exceptions import CMSError
from cms.models import RaesPackageSource
from cms.services import (
    PackRegistrationRequest,
    RegisteredPack,
    register_pack,
    set_catalog_publication,
    validate_registered_pack_conformance,
)
from cms.services._content_authority import require_content_scope, require_native_content_credential
from cms.services._content_ingestion import prepare_pack_registration
from shared.credentials import CredentialContext
from shared.identity_scope import ResourceScope
from shared.schemas.raes_package_source import (
    PackageSourceRecord,
    RaesPackageSourceError,
    validate_package_source,
)

# The declared in-box pack manifest that ships with Shifter.
SHIPPED_INBOX_MANIFEST = Path(__file__).parent / "inbox_packs" / "manifest.yaml"

_ENTRY_FIELDS = frozenset(
    {
        "scenario_id",
        "source_kind",
        "contract_kind",
        "contract_profile",
        "package_ref",
        "package_version",
        "package_digest",
        "expected_package_digest",
        "lock_ref",
        "lock_digest",
        "provenance",
    }
)
_TEXT_FIELD_LIMITS = {
    "scenario_id": 100,
    "source_kind": 16,
    "contract_kind": 32,
    "contract_profile": 128,
    "package_ref": 512,
    "package_version": 128,
    "package_digest": 71,
    "expected_package_digest": 71,
    "lock_ref": 512,
    "lock_digest": 71,
}


class InboxManifestError(CMSError):
    """Raised when the in-box seed declaration is absent or malformed."""


def load_inbox_manifest(manifest_path: Path | None = None) -> list[PackRegistrationRequest]:
    """Parse and validate the complete in-box declaration, or fail closed."""
    path = Path(manifest_path) if manifest_path is not None else SHIPPED_INBOX_MANIFEST
    if not path.is_file():
        raise InboxManifestError("in-box pack manifest is missing")
    try:
        with path.open(encoding="utf-8") as handle:
            doc = yaml.safe_load(handle)
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise InboxManifestError("in-box pack manifest is unreadable or invalid YAML") from exc
    if not isinstance(doc, dict) or set(doc) != {"packs"}:
        raise InboxManifestError("in-box pack manifest must contain exactly one 'packs' declaration")
    packs = doc["packs"]
    if not isinstance(packs, list):
        raise InboxManifestError("in-box pack manifest 'packs' declaration must be a list")

    requests: list[PackRegistrationRequest] = []
    for index, entry in enumerate(packs):
        if not isinstance(entry, dict):
            raise InboxManifestError(f"in-box pack manifest entry {index} must be a mapping")
        requests.append(_entry_to_request(entry, index=index))
    return requests


def register_inbox_packs(
    *, actor: CredentialContext, manifest_path: Path | None = None, request_id: str = ""
) -> list[RegisteredPack]:
    """Atomically register one native manifest, then qualify and publish exact identities.

    All native admission happens before the registration transaction. Qualification
    or publication failure preserves pending or unpublished rows for an exact retry.
    No manifest origin receives a separate validation or authorization path.
    """
    if transaction.get_connection().in_atomic_block:
        raise InboxManifestError("Catalog initialization requires its own transaction boundary")
    actor = require_native_content_credential(actor)
    scope = ResourceScope("installation")
    actor = require_content_scope(actor, scope, "read_content")
    actor = require_content_scope(actor, scope, "publish_content")
    requests = load_inbox_manifest(manifest_path)
    admitted = []
    for request in requests:
        if (
            request.expected_package_digest
            and not RaesPackageSource.objects.filter(scenario_id=request.scenario_id).exists()
        ):
            request = replace(request, expected_package_digest="")
        admitted.append((request, prepare_pack_registration(actor, request, scope)))
    registered: list[RegisteredPack] = []
    with transaction.atomic():
        for request, admission in admitted:
            result = register_pack(
                user=admission.credential,
                request=request,
                request_id=request_id,
                idempotent=True,
                scope=scope,
                _prepared_admission=admission,
            )
            if result.created:
                registered.append(result)
    for request, _ in admitted:
        source = RaesPackageSource.objects.get(scenario_id=request.scenario_id)
        if source.package_digest != request.package_digest:
            raise InboxManifestError("in-box pack identity changed before qualification")
        if source.conformance_status != RaesPackageSource.ConformanceStatus.PASSED:
            validate_registered_pack_conformance(
                user=actor,
                scenario_id=request.scenario_id,
                expected_package_digest=request.package_digest,
            )
        set_catalog_publication(
            actor,
            request.scenario_id,
            published=True,
            expected_package_digest=request.package_digest,
        )
    return registered


def _entry_to_request(entry: dict[str, Any], *, index: int) -> PackRegistrationRequest:
    """Build one request after strict declaration and shared-contract validation."""
    if not set(entry).issubset(_ENTRY_FIELDS):
        raise InboxManifestError(f"in-box pack manifest entry {index} has unsupported fields")
    request = PackRegistrationRequest(
        scenario_id=entry.get("scenario_id", ""),
        source_kind=entry.get("source_kind", "repo"),
        contract_kind=entry.get("contract_kind", "raes"),
        contract_profile=entry.get("contract_profile", "shifter"),
        package_ref=entry.get("package_ref", ""),
        package_version=entry.get("package_version", ""),
        package_digest=entry.get("package_digest", ""),
        expected_package_digest=entry.get("expected_package_digest", ""),
        lock_ref=entry.get("lock_ref", ""),
        lock_digest=entry.get("lock_digest", ""),
        provenance=entry.get("provenance", {}),
    )
    for field, limit in _TEXT_FIELD_LIMITS.items():
        value = getattr(request, field)
        if not isinstance(value, str) or len(value) > limit:
            raise InboxManifestError(f"in-box pack manifest entry {index} has an invalid {field}")
    try:
        validate_slug(request.scenario_id)
    except ValidationError as exc:
        raise InboxManifestError(f"in-box pack manifest entry {index} has an invalid scenario_id") from exc
    if not isinstance(request.provenance, dict):
        raise InboxManifestError(f"in-box pack manifest entry {index} has invalid provenance")
    try:
        validate_package_source(
            PackageSourceRecord(
                source_kind=request.source_kind,
                contract_kind=request.contract_kind,
                contract_profile=request.contract_profile,
                package_ref=request.package_ref,
                package_version=request.package_version,
                package_digest=request.package_digest,
                conformance_status="pending",
                lock_ref=request.lock_ref,
                lock_digest=request.lock_digest,
                provenance=request.provenance,
            )
        )
    except RaesPackageSourceError as exc:
        raise InboxManifestError(f"in-box pack manifest entry {index} violates the package-source contract") from exc
    return request
