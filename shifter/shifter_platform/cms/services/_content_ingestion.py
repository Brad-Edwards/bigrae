"""Uniform, entitlement-blind content ingestion (#1578, ADR-034).

One CMS service boundary registers a pack, identically for shipped, public,
private, and self-authored packs. There is no "built-in" / "imported" /
"authored" fork: ``source_kind`` is a *resolver* fact, not a provenance one, and
nothing here branches on how the operator obtained the pack.

The boundary:

- **authorizes the registration scope and reference custody** through current
  native policy and exact server records; generic references require separate
  installation content authority. Entitlement is resolved out-of-band (ADR-034);
- **validates the incoming pack as foreign input** (``cms.scenarios.pack_validation``)
  so broken / malformed / non-conformant content is rejected fail-closed;
- **fails closed on duplicate RAES identities**;
- **keeps registration separate from conformance**: object-backed packs become
  launchable only after their immutable source passes the shared conformance
  gate through the containment-checked object resolver;
- **persists a provenance-only reference** (:class:`cms.models.RaesPackageSource`,
  whose ``save`` enforces the reference-only contract) and **audits** the result
  with sanitized fields only.

Every caller — the in-box bootstrap, the operator management command, and the
DRF authoring endpoint — uses :func:`register_pack`; there is no privileged code
path for the in-box seed.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import UUID

from django.conf import settings
from django.db import IntegrityError, transaction

from cms.exceptions import CMSError
from cms.models import RaesPackageSource
from cms.scenarios.pack_validation import (
    PackDigestError,
    PackValidationError,
    validate_pack,
    verify_pack_digest,
)
from shared.audit import AuditAction, AuditEntityType, AuditEvent, audit_log, principal_actor_fields
from shared.credentials import CredentialContext
from shared.identity_scope import ResourceScope
from shared.log_sanitize import safe_log_value
from shared.schemas.raes_package_source import RaesPackageSourceError

from ._content_authority import require_native_content_credential
from ._pack_feature_acquisition import _request_feature_artifacts
from ._pack_registration_custody import (
    prepare_registration_reference,
)
from ._pack_registration_fences import (
    _fence_registration_credential,
    _fence_registration_reference,
    _fence_registration_scope,
    _registration_scope_matches,
)
from ._pack_registration_types import (
    PackRegistrationAdmission,
    PackRegistrationRequest,
    RegisteredPack,
    _registration_digest,
)

if TYPE_CHECKING:
    from typing import Any

    from django.contrib.auth.models import User

    from ._tenant_pack_upload_journal import PackUploadJournal


_PACK_REGISTRATION_IS_UNAVAILABLE = "Pack registration is unavailable"


logger = logging.getLogger(__name__)

_OBJECT_SOURCE_KIND = "object"

# Registration is not conformance. Conformance ("passed") is a trust fact
# established by a separate, trusted conformance process (the RAES conformance
# gate), never asserted by the registering caller — otherwise an API/CLI caller
# could promote its own claim into the registry's authoritative launchability
# decision. Every registration therefore lands non-passed, and conformance is
# promoted out of band. ``conformance_status`` / ``conformance_report_ref`` are
# deliberately NOT part of the registration request.
_REGISTRATION_CONFORMANCE_STATUS = "pending"


def prepare_pack_registration(
    credential: CredentialContext,
    request: PackRegistrationRequest,
    scope: ResourceScope,
    *,
    _upload_journal: PackUploadJournal | None = None,
) -> PackRegistrationAdmission:
    """Admit exact already-validated registration inputs before an enclosing aggregate write."""
    from ._content_authority import require_content_scope

    current = require_content_scope(credential, scope, "manage_content")
    _authorize_registration(current, request, scope.organization_uuid, scope)
    reference = prepare_registration_reference(current, request, scope, _upload_journal)
    return PackRegistrationAdmission(current, scope, _registration_digest(request), reference)


def _authorize_registration(
    user: CredentialContext,
    request: PackRegistrationRequest,
    organization_uuid: UUID | None,
    scope: ResourceScope | None = None,
    prepared: PackRegistrationAdmission | None = None,
) -> UUID | None:
    """Authorize the ingestion scope and validate caller-supplied revision identity."""
    user = require_native_content_credential(user)
    from ._content_authority import require_content_scope

    if scope is None:
        raise CMSError(_PACK_REGISTRATION_IS_UNAVAILABLE)
    if prepared is None:
        require_content_scope(user, scope, "manage_content")
    elif (
        prepared.credential != user
        or prepared.scope != scope
        or prepared.request_digest != _registration_digest(request)
    ):
        raise CMSError(_PACK_REGISTRATION_IS_UNAVAILABLE)
    if organization_uuid is not None and organization_uuid != scope.organization_uuid:
        raise CMSError(_PACK_REGISTRATION_IS_UNAVAILABLE)
    organization_uuid = scope.organization_uuid
    if request.expected_package_digest and not re.fullmatch(r"sha256:[a-f0-9]{64}", request.expected_package_digest):
        raise CMSError("Invalid expected package digest")
    return organization_uuid


def register_pack(
    *,
    user: User | CredentialContext,
    request: PackRegistrationRequest,
    request_id: str = "",
    idempotent: bool = False,
    organization_uuid: UUID | None = None,
    scope: ResourceScope | None = None,
    _prepared_admission: PackRegistrationAdmission | None = None,
) -> RegisteredPack:
    """Register a pack through the single uniform ingestion boundary.

    Args:
        user: The actor. Must pass the CMS authoring authorization gate. This is
            the WHO-may-register check, not an entitlement-to-acquire check.
        request: The source-agnostic registration input.
        request_id: Optional correlation id for the audit record.
        idempotent: Return ``created=False`` when the same immutable request is
            already registered. Any drift remains a conflict. Used by deploy
            bootstrap retries; ordinary API/CLI duplicates still fail.

    Returns:
        A bounded :class:`RegisteredPack` summary.

    Raises:
        TypeError / django PermissionDenied: from the authorization gate.
        CMSError: on a duplicate id, an invalid pack, a
            scenario_id that does not match the pack's validated identity, or an
            invalid reference record.
    """
    user = require_native_content_credential(user)
    if scope is None:
        raise CMSError(_PACK_REGISTRATION_IS_UNAVAILABLE)
    if _prepared_admission is None:
        _prepared_admission = prepare_pack_registration(user, request, scope)
        user = _prepared_admission.credential
    organization_uuid = _authorize_registration(user, request, organization_uuid, scope, _prepared_admission)
    from ._content_authority import content_scope_fields

    custody = {**content_scope_fields(scope), "owner_principal_uuid": user.principal.uuid}
    existing = RaesPackageSource.objects.filter(scenario_id=request.scenario_id).first()
    if existing is not None:
        if not _registration_scope_matches(existing, organization_uuid, scope):
            raise CMSError(_PACK_REGISTRATION_IS_UNAVAILABLE)
        return _existing_registration(
            existing,
            user,
            request,
            request_id=request_id,
            idempotent=idempotent,
            scope=scope,
            prepared=_prepared_admission,
        )
    if request.expected_package_digest:
        raise CMSError("Pack revision conflicts with the current immutable identity")
    _bind_scenario_id_to_pack_identity(request)
    _authorize_registration(user, request, organization_uuid, scope, _prepared_admission)

    try:
        with transaction.atomic():
            _fence_registration_credential(user)
            _fence_registration_reference(user, request, scope, _prepared_admission)
            row = RaesPackageSource.objects.create(
                scenario_id=request.scenario_id,
                **custody,
                package_name=request.package_name,
                source_kind=request.source_kind,
                contract_kind=request.contract_kind,
                contract_profile=request.contract_profile,
                package_ref=request.package_ref,
                package_version=request.package_version,
                # The registration boundary verified this canonical digest
                # against the exact local pack inventory and payload bytes.
                # Launch re-verifies it immediately before loading SDL.
                package_digest=request.package_digest,
                lock_ref=request.lock_ref,
                lock_digest=request.lock_digest,
                # Conformance is never caller-asserted at registration; a trusted
                # conformance process promotes it out of band.
                conformance_status=_REGISTRATION_CONFORMANCE_STATUS,
                conformance_report_ref="",
                provenance=dict(request.provenance or {}),
                registered_by=None,
            )
            _audit_registration(row, user, request_id)
    except RaesPackageSourceError as exc:
        # Reference-shape violation from the model's provenance-only validator.
        raise CMSError("Invalid pack reference") from exc
    except IntegrityError as exc:
        return _registration_race(
            request, organization_uuid, idempotent, exc, scope=scope, user=user, prepared=_prepared_admission
        )

    logger.info(
        "register_pack: registered scenario_id=%s source_kind=%s",
        safe_log_value(row.scenario_id),
        row.source_kind,
    )
    scenario_id = row.scenario_id
    transaction.on_commit(lambda: _request_feature_artifacts(scenario_id))
    return RegisteredPack(
        scenario_id=row.scenario_id,
        source_kind=row.source_kind,
        contract_kind=row.contract_kind,
        contract_profile=row.contract_profile,
        conformance_status=row.conformance_status,
        created=True,
    )


def _registration_race(
    request: PackRegistrationRequest,
    organization_uuid: UUID | None,
    idempotent: bool,
    exc: IntegrityError,
    *,
    scope: ResourceScope | None = None,
    user: CredentialContext,
    prepared: PackRegistrationAdmission,
) -> RegisteredPack:
    """Reuse only an identical concurrent winner in the same organization scope."""
    if idempotent:
        with transaction.atomic():
            _fence_registration_scope(user, scope)
            raced = RaesPackageSource.objects.select_for_update().filter(scenario_id=request.scenario_id).first()
            _fence_registration_reference(user, request, scope, prepared)
            if raced is not None and _registration_scope_matches(raced, organization_uuid, scope):
                return _reuse_existing(raced, request, idempotent=True)
    raise CMSError(f"A pack with id '{request.scenario_id}' is already registered") from exc


def _existing_registration(
    existing: RaesPackageSource,
    user: CredentialContext,
    request: PackRegistrationRequest,
    *,
    request_id: str,
    idempotent: bool,
    scope: ResourceScope | None = None,
    prepared: PackRegistrationAdmission | None = None,
) -> RegisteredPack:
    """Handle existing registration."""
    if request.expected_package_digest:
        return _replace_existing(
            user, request, request_id=request_id, idempotent=idempotent, scope=scope, prepared=prepared
        )
    with transaction.atomic():
        _fence_registration_scope(user, scope)
        row = RaesPackageSource.objects.select_for_update().filter(pk=existing.pk).first()
        if row is None:
            raise CMSError(_PACK_REGISTRATION_IS_UNAVAILABLE)
        _fence_registration_reference(user, request, scope, prepared)
        if not _registration_scope_matches(row, existing.organization_uuid, scope):
            raise CMSError(_PACK_REGISTRATION_IS_UNAVAILABLE)
        return _reuse_existing(row, request, idempotent=idempotent)


_IDENTITY_FIELDS = (
    "package_name",
    "source_kind",
    "contract_kind",
    "contract_profile",
    "package_ref",
    "package_version",
    "package_digest",
    "lock_ref",
    "lock_digest",
    "provenance",
)


def _identity(value: RaesPackageSource | PackRegistrationRequest) -> dict[str, object]:
    """Capture bounded reference identity, including the optional provenance map."""
    return {
        name: (getattr(value, name) or {} if name == "provenance" else getattr(value, name))
        for name in _IDENTITY_FIELDS
    }


def _replace_existing(
    user: CredentialContext,
    request: PackRegistrationRequest,
    *,
    request_id: str,
    idempotent: bool,
    scope: ResourceScope | None = None,
    prepared: PackRegistrationAdmission | None = None,
) -> RegisteredPack:
    """Admit an explicit new version while preserving prior reference evidence.

    In-flight range inputs are immutable snapshots owned by the engine; this
    service changes only the catalog's current source. Conformance must be
    established again for the new digest through the existing trusted process.
    """
    _bind_scenario_id_to_pack_identity(request)
    if scope is None:
        raise CMSError(_PACK_REGISTRATION_IS_UNAVAILABLE)
    _authorize_registration(user, request, scope.organization_uuid, scope, prepared)
    with transaction.atomic():
        _fence_registration_scope(user, scope)
        row = RaesPackageSource.objects.select_for_update().filter(scenario_id=request.scenario_id).first()
        if row is None:
            raise CMSError(_PACK_REGISTRATION_IS_UNAVAILABLE)
        _fence_registration_credential(user)
        _fence_registration_reference(user, request, scope, prepared)
        if not _registration_scope_matches(row, scope.organization_uuid if scope else row.organization_uuid, scope):
            raise CMSError(_PACK_REGISTRATION_IS_UNAVAILABLE)
        if _identity(row) == _identity(request):
            return _reuse_existing(row, request, idempotent=idempotent)
        if _revision_conflicts(row, request):
            raise CMSError("Pack revision conflicts with the current immutable identity")
        previous = _identity(row)
        for name, value in _identity(request).items():
            setattr(row, name, value)
        row.conformance_status = _REGISTRATION_CONFORMANCE_STATUS
        row.conformance_report_ref = ""
        row.registered_by = None
        try:
            row.save()
        except RaesPackageSourceError as exc:
            raise CMSError("Invalid pack reference") from exc
        try:
            audit_log(
                AuditEvent(
                    entity_type=AuditEntityType.SCENARIO,
                    entity_id=0,
                    entity_ref=row.scenario_id,
                    action=AuditAction.UPDATE,
                    **_registration_actor(user),
                    request_id=request_id,
                    previous_state={"scenario_id": row.scenario_id, **previous},
                    new_state={"scenario_id": row.scenario_id, **_identity(row), "conformance_status": "pending"},
                ),
                strict=True,
            )
        except Exception as exc:
            raise CMSError("pack revision audit failed") from exc
        scenario_id = row.scenario_id
        transaction.on_commit(lambda: _request_feature_artifacts(scenario_id))
        return RegisteredPack(
            row.scenario_id,
            row.source_kind,
            row.contract_kind,
            row.contract_profile,
            row.conformance_status,
            created=False,
        )


def _reuse_existing(
    row: RaesPackageSource,
    request: PackRegistrationRequest,
    *,
    idempotent: bool,
) -> RegisteredPack:
    """Return an exact idempotent retry or reject duplicate/drifted identity."""
    if not idempotent:
        raise CMSError(f"A pack with id '{request.scenario_id}' is already registered")
    if _identity(row) != _identity(request):
        raise CMSError(f"A pack with id '{request.scenario_id}' is already registered with different identity")
    # A no-op retry still validates the referenced bytes. Otherwise a mutable
    # repo pack could drift while bootstrap silently reported success.
    _bind_scenario_id_to_pack_identity(request)
    return RegisteredPack(
        scenario_id=row.scenario_id,
        source_kind=row.source_kind,
        contract_kind=row.contract_kind,
        contract_profile=row.contract_profile,
        conformance_status=row.conformance_status,
        created=False,
    )


def _bind_scenario_id_to_pack_identity(request: PackRegistrationRequest) -> None:
    """Validate a resolvable (repo) pack and bind the catalog id to its identity.

    For repo packs the pack is validated as foreign input, the caller's
    ``scenario_id`` MUST equal the pack's validated identity, and the advertised
    digest MUST match the canonical RAES associated-artifact identity. Staging
    is required to remain immutable; launch repeats the byte binding immediately
    before SDL load so a later replacement fails closed.

    Object-backed packs are resolved and identity-bound by the shared conformance
    service before promotion. The launch resolver repeats the same source and
    byte binding before use; registration itself performs no object download.
    """
    if request.source_kind == _OBJECT_SOURCE_KIND:
        return
    pack_root = _resolve_repo_pack_root(request.package_ref)
    try:
        validated_name = validate_pack(pack_root)
    except PackValidationError as exc:
        logger.warning(
            "register_pack: pack failed validation scenario_id=%s reasons=%s",
            safe_log_value(request.scenario_id),
            safe_log_value(str(exc)),
        )
        raise CMSError("pack failed ingestion validation") from exc
    if validated_name != (request.package_name or request.scenario_id):
        raise CMSError("scenario_id does not match the pack's validated identity")
    try:
        digest_matches = verify_pack_digest(pack_root, request.package_digest)
    except PackDigestError as exc:
        logger.warning(
            "register_pack: pack digest could not be verified scenario_id=%s",
            safe_log_value(request.scenario_id),
        )
        raise CMSError("pack content digest could not be verified") from exc
    if not digest_matches:
        raise CMSError("package_digest does not match the validated pack content")


def _resolve_repo_pack_root(package_ref: str) -> Path:
    """Resolve a repo-relative package_ref to a contained pack root directory."""
    root = Path(settings.RAES_PACKAGE_ROOT).resolve()
    candidate = (root / package_ref).resolve()
    if candidate != root and root not in candidate.parents:
        raise CMSError("package_ref escapes the configured package root")
    return candidate


def _audit_registration(row: RaesPackageSource, user: CredentialContext, request_id: str) -> None:
    """Record the sanitized registration audit as a fail-closed safety control."""
    try:
        audit_log(
            AuditEvent(
                entity_type=AuditEntityType.SCENARIO,
                entity_id=0,
                entity_ref=row.scenario_id,
                action=AuditAction.CREATE,
                **_registration_actor(user),
                new_state={
                    "scenario_id": row.scenario_id,
                    "source_kind": row.source_kind,
                    "contract_kind": row.contract_kind,
                    "contract_profile": row.contract_profile,
                    "package_digest": row.package_digest,
                    "conformance_status": row.conformance_status,
                    "result": "registered",
                },
                request_id=request_id,
            ),
            strict=True,
        )
    except Exception as exc:
        raise CMSError("pack registration audit failed") from exc


def _registration_actor(user: CredentialContext) -> dict[str, Any]:
    """Select principal attribution without fabricating a historical user."""
    return dict(principal_actor_fields(require_native_content_credential(user).principal))


def _revision_conflicts(row: RaesPackageSource, request: PackRegistrationRequest) -> bool:
    """A revision must replace the expected digest and introduce a distinct version."""
    return row.package_digest != request.expected_package_digest or row.package_version == request.package_version
