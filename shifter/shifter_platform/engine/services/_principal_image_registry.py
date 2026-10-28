"""Explicit installation image administration under the canonical preparation action."""

from django.db import transaction

from shared.audit import AuditAction, AuditEntityType, AuditEvent, audit_log, principal_actor_fields
from shared.authorization import AuthorizationRequest, TargetRef
from shared.authorization.policy import require_authorization
from shared.credentials import resolve_current_credential, resolve_local_credential
from shared.identity_scope import ResourceScope


def authorize_image_registry(credential):
    """Evaluate live policy before any image lookup or write transaction."""
    try:
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
    except Exception:
        raise PermissionError("Image registry unavailable") from None


def operator_image_selection():
    """Only native installation inventory belongs to this explicit global registry."""
    return {
        "scope_kind": "installation",
        "account_uuid": None,
        "organization_uuid": None,
        "workspace_uuid": None,
    }


def _fence_image_credential(current):
    try:
        if resolve_local_credential(current) != current:
            raise ValueError("Image registry authority changed")
    except ValueError:
        raise PermissionError("Image registry unavailable") from None


def upsert_operator_image(credential, *, key, defaults):
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
            raise PermissionError("Image registry unavailable")
        row, _ = RaesImageMapping.objects.update_or_create(
            **selection,
            defaults=defaults,
            create_defaults={
                **defaults,
                "owner_principal_uuid": current.principal.uuid,
                "catalog_publication_scope": "operator",
            },
        )
        _audit_image(row, current)
        return row


def disable_operator_image(credential, *, key):
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
            raise PermissionError("Image registry unavailable")
        row.enabled = False
        row.save(update_fields=["enabled", "updated_at"])
        _audit_image(row, current)
        return row


def _audit_image(row, current):
    audit_log(
        AuditEvent(
            entity_type=AuditEntityType.CONFIG,
            entity_id=row.pk,
            entity_ref=str(row.pk),
            action=AuditAction.UPDATE,
            **principal_actor_fields(current.principal),
            new_state={"image_mapping_id": row.pk, "enabled": row.enabled},
            context="installation_image_registry",
        ),
        strict=True,
    )
