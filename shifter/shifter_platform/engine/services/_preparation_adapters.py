"""Explicit, audited installation of private and shipped preparation adapters."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from uuid import UUID

from django.db import transaction
from pydantic import ValidationError as ModelValidationError

from shared.audit import (
    AuditAction,
    AuditActorType,
    AuditEntityType,
    AuditEvent,
    RequestAudit,
    audit_log,
    principal_actor_fields,
)
from shared.authorization import AuthorizationRequest, TargetRef
from shared.authorization.policy import require_authorization
from shared.credentials import CredentialContext, resolve_current_credential
from shared.exceptions import ValidationError
from shared.identity_scope import ResourceScope
from shared.preparation_grant import PreparationGrantConfiguration
from shared.raes.preparation_contract import AdapterManifest

if TYPE_CHECKING:
    from django.contrib.auth.models import User

    from engine.models import PreparationAdapter, PreparationGrant


@dataclass(frozen=True)
class PreparationAdapterView:
    """Private administrative projection, returned only after actor authorization."""

    id: UUID
    grant_id: UUID
    scope_digest: str
    manifest_digest: str
    manifest: dict[str, Any]
    state: str


def require_preparation_permission(user: User | CredentialContext, permission: str) -> CredentialContext | None:
    """Enforce explicit application authority for CLI, API and service callers."""
    if isinstance(user, CredentialContext):
        actions = {
            "manage_preparation_adapters": "installation.manage_adapters",
            "manage_preparation_grants": "installation.manage_adapters",
            "prepare_artifacts": "installation.prepare_content",
        }
        try:
            current = resolve_current_credential(user)
            require_authorization(
                AuthorizationRequest(
                    current.principal,
                    actions[permission],
                    TargetRef("installation"),
                    ResourceScope("installation"),
                    current.ceiling,
                )
            )
            return current
        except Exception:
            raise ValidationError("Artifact preparation access is not permitted") from None
    if not (user and user.is_authenticated and user.is_active and user.has_perm(f"engine.{permission}")):
        raise ValidationError("Artifact preparation access is not permitted")
    return None


def install_preparation_adapter(
    user: User | CredentialContext, grant_id: UUID, payload: object, *, audit: RequestAudit | None = None
) -> PreparationAdapterView:
    """Install an exact version under an existing cloud grant, with strict audit.

    Registering a pack never calls this service. A retry may return an identical
    registration, but cannot re-enable it or change any executable/grant identity.
    """
    from engine.models import PreparationAdapter

    current = require_preparation_permission(user, "manage_preparation_adapters")
    try:
        manifest = AdapterManifest.model_validate(payload)
    except ModelValidationError as exc:
        # Pydantic errors carry input values, including private registry names.
        raise ValidationError("The adapter manifest is invalid") from exc
    with transaction.atomic():
        grant = _locked_grant(grant_id)
        _validate_grant(grant, manifest)
        row, created = PreparationAdapter.objects.get_or_create(
            scope_digest=grant.scope_digest,
            adapter_id=manifest.adapter_id,
            version=manifest.version,
            defaults={
                "grant": grant,
                "manifest_digest": manifest.digest,
                "manifest": manifest.model_dump(mode="json"),
                "installed_by": None if current else user,
                "installed_by_principal_uuid": current.principal.uuid if current else None,
            },
        )
        if row.manifest_digest != manifest.digest or row.grant_id != grant.id:
            raise ValidationError("An installed adapter version cannot change its manifest or grant")
        if created:
            _audit_adapter(user, row, "install", audit=audit)
        return _view(row)


def set_preparation_adapter_state(
    user: User | CredentialContext, adapter_id: UUID, state: str, *, audit: RequestAudit | None = None
) -> PreparationAdapterView:
    """Fence new starts without retargeting in-flight execution or cleanup."""
    from engine.models import PreparationAdapter

    require_preparation_permission(user, "manage_preparation_adapters")
    if state not in PreparationAdapter.State.values:
        raise ValidationError("The adapter lifecycle state is invalid")
    with transaction.atomic():
        initial = PreparationAdapter.objects.filter(pk=adapter_id).first()
        if initial is None:
            raise ValidationError("The adapter is unavailable")
        grant = _locked_grant(initial.grant_id)
        row = PreparationAdapter.objects.select_for_update().get(pk=adapter_id)
        if row.state == PreparationAdapter.State.RETIRED and state != row.state:
            raise ValidationError("A retired adapter version cannot be reactivated")
        if state == PreparationAdapter.State.ENABLED:
            _validate_grant(grant, AdapterManifest.model_validate(row.manifest))
        if row.state != state:
            previous = row.state
            row.state = state
            row.save(update_fields=["state", "updated_at"])
            _audit_adapter(user, row, state, previous=previous, audit=audit)
        return _view(row)


def list_preparation_adapters(user: User | CredentialContext) -> list[PreparationAdapterView]:
    """Return private registration detail only to executable administrators."""
    from engine.models import PreparationAdapter

    require_preparation_permission(user, "manage_preparation_adapters")
    return [_view(row) for row in PreparationAdapter.objects.order_by("adapter_id", "version")]


def list_preparation_adapter_grants(user: User | CredentialContext) -> list[dict[str, Any]]:
    """List selectable grants without exposing cloud credentials or configuration."""
    from engine.models import PreparationGrant

    require_preparation_permission(user, "manage_preparation_adapters")
    return [
        {"id": row.id, "active": row.active, "verified_at": row.verified_at}
        for row in PreparationGrant.objects.order_by("created_at")
    ]


def _locked_grant(grant_id: UUID) -> PreparationGrant:
    """Lock authority before registration/operation locks to serialize revocation."""
    from engine.models import PreparationGrant

    grant = PreparationGrant.objects.select_for_update().filter(pk=grant_id).first()
    if grant is None:
        raise ValidationError("The preparation grant is unavailable")
    return grant


def _validate_grant(grant: PreparationGrant, manifest: AdapterManifest) -> PreparationGrantConfiguration:
    """Requested capabilities must fit the separately installed cloud authority."""
    try:
        configuration = PreparationGrantConfiguration.model_validate(grant.configuration)
    except ModelValidationError as exc:
        raise ValidationError("The preparation grant configuration is invalid") from exc
    if (
        not grant.active
        or configuration.digest != grant.configuration_digest
        or configuration.scope_digest != grant.scope_digest
        or configuration.backend != manifest.backend
        or not set(manifest.required_permissions).issubset(configuration.permissions)
        or manifest.worker_image not in configuration.approved_worker_images
        or manifest.verifier_image not in configuration.approved_verifier_images
    ):
        raise ValidationError("The preparation grant does not authorize this adapter")
    return configuration


def validate_pinned_adapter(row: PreparationAdapter) -> AdapterManifest:
    """Stored executable identity must still match its immutable registration."""
    try:
        manifest = AdapterManifest.model_validate(row.manifest)
    except ModelValidationError as exc:
        raise ValidationError("The preparation adapter registration is invalid") from exc
    if (
        manifest.digest != row.manifest_digest
        or manifest.adapter_id != row.adapter_id
        or manifest.version != row.version
        or row.scope_digest != row.grant.scope_digest
    ):
        raise ValidationError("The preparation adapter registration is invalid")
    return manifest


def _audit_adapter(
    user: User | CredentialContext,
    row: PreparationAdapter,
    action: str,
    *,
    previous: str = "",
    audit: RequestAudit | None = None,
) -> None:
    """Commit bounded identities, never private manifests, images or credentials."""
    attribution = audit or RequestAudit()
    actor = (
        principal_actor_fields(user.principal)
        if isinstance(user, CredentialContext)
        else {
            "actor_type": attribution.actor_type or AuditActorType.USER,
            "actor_id": attribution.actor_id if attribution.actor_type else user.id,
        }
    )
    audit_log(
        AuditEvent(
            entity_type=AuditEntityType.PREPARATION_ADAPTER,
            entity_id=0,
            entity_ref=str(row.id),
            action=AuditAction.CREATE if action == "install" else AuditAction.UPDATE,
            **actor,
            request_id=attribution.request_id,
            source_ip=attribution.source_ip,
            user_agent=attribution.user_agent,
            previous_state={"state": previous} if previous else {},
            new_state={"id": str(row.id), "state": row.state, "manifest_digest": row.manifest_digest},
        ),
        strict=True,
    )


def _view(row: PreparationAdapter) -> PreparationAdapterView:
    """Keep ORM state behind the engine service boundary."""
    return PreparationAdapterView(row.id, row.grant_id, row.scope_digest, row.manifest_digest, row.manifest, row.state)
