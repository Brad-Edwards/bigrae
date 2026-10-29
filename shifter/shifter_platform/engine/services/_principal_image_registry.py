"""Explicit installation image administration under the canonical preparation action."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

from django.db import transaction

from shared.audit import AuditAction, AuditEntityType, AuditEvent, audit_log, principal_actor_fields
from shared.authorization import AuthorizationRequest, TargetRef
from shared.authorization.policy import require_authorization
from shared.credentials import CredentialContext, resolve_current_credential, resolve_local_credential
from shared.identity_scope import ResourceScope

if TYPE_CHECKING:
    from engine.models import RaesImageMapping

_IMAGE_REGISTRY_UNAVAILABLE = "Image registry unavailable"


def authorize_image_registry(credential: object) -> CredentialContext:
    """Evaluate live policy before any image lookup or write transaction."""
    try:
        if not isinstance(credential, CredentialContext):
            raise ValueError("Native credential required")
        current = resolve_current_credential(credential)
        require_authorization(
            AuthorizationRequest(
                current.principal,
                "installation.prepare_content",
                TargetRef("installation"),
                ResourceScope("installation"),
                current.ceiling,
            )
        )
        return current
    except Exception:  # noqa: BLE001  # fail-closed authority boundary; suppress private causes
        raise PermissionError(_IMAGE_REGISTRY_UNAVAILABLE) from None


def operator_image_selection() -> dict[str, str | None]:
    """Only native installation inventory belongs to this explicit global registry."""
    return {
        "scope_kind": "installation",
        "account_uuid": None,
        "organization_uuid": None,
        "workspace_uuid": None,
    }


def _fence_image_credential(current: CredentialContext) -> None:
    """Refuse changed local proof before the image mutation commits."""
    try:
        if resolve_local_credential(current) != current:
            raise ValueError("Image registry authority changed")
    except ValueError:
        raise PermissionError(_IMAGE_REGISTRY_UNAVAILABLE) from None


def upsert_operator_image(
    credential: object, *, key: Mapping[str, object], defaults: Mapping[str, object]
) -> RaesImageMapping:
    """Retain immutable ownership and commit current-proof fencing with strict audit."""
    from engine.models import PreparedArtifactAdmission, RaesImageMapping

    current = authorize_image_registry(credential)
    with transaction.atomic():
        _fence_image_credential(current)
        selection = {**operator_image_selection(), **key}
        existing = RaesImageMapping.objects.select_for_update().filter(**selection).first()
        if existing is not None and (
            existing.owner_principal_uuid is None
            or PreparedArtifactAdmission.objects.filter(image_mapping=existing).exists()
        ):
            raise PermissionError(_IMAGE_REGISTRY_UNAVAILABLE)
        previous_state = _mapping_audit_state(existing) if existing is not None else None
        row, _ = RaesImageMapping.objects.update_or_create(
            **selection,
            defaults=defaults,
            create_defaults={
                **defaults,
                "owner_principal_uuid": current.principal.uuid,
                "catalog_publication_scope": "operator",
            },
        )
        _audit_image(row, current, previous_state)
        return row


def disable_operator_image(credential: object, *, key: Mapping[str, object]) -> RaesImageMapping:
    """Disable only the exact scoped row; historical unscoped aliases remain isolated."""
    from engine.models import RaesImageMapping

    current = authorize_image_registry(credential)
    with transaction.atomic():
        row = RaesImageMapping.objects.select_for_update().filter(**operator_image_selection(), **key).first()
        _fence_image_credential(current)
        if row is None:
            from ._raes_image import RaesImageMappingError

            raise RaesImageMappingError("Image mapping unavailable")
        if row.owner_principal_uuid is None:
            raise PermissionError(_IMAGE_REGISTRY_UNAVAILABLE)
        previous_state = _mapping_audit_state(row)
        row.enabled = False
        row.save(update_fields=["enabled", "updated_at"])
        _audit_image(row, current, previous_state)
        return row


def _mapping_audit_state(row: RaesImageMapping) -> dict[str, object]:
    """Capture the locked image selection before or after a registry mutation."""
    return {
        "image_mapping_id": row.pk,
        "provider": row.provider,
        "source_name": row.source_name,
        "source_version": row.source_version,
        "image_ref": row.image_ref,
        "image_kind": row.image_kind,
        "enabled": row.enabled,
    }


def _audit_image(row: RaesImageMapping, current: CredentialContext, previous_state: dict[str, object] | None) -> None:
    """Commit principal attribution and exact before/after images with the mutation."""
    audit_log(
        AuditEvent(
            entity_type=AuditEntityType.CONFIG,
            entity_id=row.pk,
            entity_ref=f"raes-image-mapping:{row.provider}:{row.source_name}@{row.source_version or '*'}",
            action=AuditAction.UPDATE if previous_state is not None else AuditAction.CREATE,
            **principal_actor_fields(current.principal),
            previous_state=previous_state,
            new_state=_mapping_audit_state(row),
            context="installation_image_registry",
        ),
        strict=True,
    )
