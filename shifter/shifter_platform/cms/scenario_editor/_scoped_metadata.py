"""Principal-neutral metadata changes stay bound to the registered source."""

from django.db import transaction

from cms.models import RaesPackageSource, ScenarioMetadata
from cms.services._content_authority import content_scope, require_content_scope
from shared.audit import AuditAction, AuditEntityType, AuditEvent, audit_log, principal_actor_fields
from shared.credentials import CredentialContext, resolve_local_credential

from ._common import ScenarioEditorError


def update_scoped_metadata(
    credential: CredentialContext, scenario_id: str, *, enabled: bool | None, staff_only: bool | None
) -> ScenarioMetadata:
    """Authorize the actual parent before assessment and fence it before write."""
    from ._metadata import _assert_publishable

    try:
        _validate_metadata_flags(enabled, staff_only)
        source = RaesPackageSource.objects.filter(scenario_id=scenario_id).first()
        if source is None:
            raise ValueError("Content unavailable")
        scope = content_scope(source)
        require_content_scope(credential, scope, "manage_content")
        _assert_publishable(scenario_id, enabled=enabled, credential=credential, scope=scope)
        current = require_content_scope(credential, scope, "manage_content")
        with transaction.atomic():
            row = (
                RaesPackageSource.objects.select_for_update()
                .filter(
                    pk=source.pk,
                    owner_principal_uuid=source.owner_principal_uuid,
                    scope_generation=source.scope_generation,
                    package_digest=source.package_digest,
                )
                .first()
            )
            if row is None or content_scope(row) != scope or resolve_local_credential(current) != current:
                raise ValueError("Content changed")
            metadata, created = ScenarioMetadata.objects.get_or_create(
                scenario_id=scenario_id,
                defaults={
                    "enabled": enabled if enabled is not None else True,
                    "staff_only": staff_only if staff_only is not None else False,
                    "updated_by_principal_uuid": current.principal.uuid,
                },
            )
            if not created:
                _save_metadata_flags(metadata, enabled, staff_only, current)
            audit_log(
                AuditEvent(
                    entity_type=AuditEntityType.SCENARIO,
                    entity_id=0,
                    entity_ref=scenario_id,
                    action=AuditAction.UPDATE,
                    **principal_actor_fields(current.principal),
                    new_state={"enabled": metadata.enabled, "staff_only": metadata.staff_only},
                ),
                strict=True,
            )
            return metadata
    except ScenarioEditorError:
        raise
    except Exception:
        raise ScenarioEditorError("Scenario metadata unavailable") from None


def _validate_metadata_flags(enabled: bool | None, staff_only: bool | None) -> None:
    """Reject nonboolean flags without coercing caller input."""
    if any(value is not None and type(value) is not bool for value in (enabled, staff_only)):
        raise ValueError("Invalid metadata")


def _save_metadata_flags(
    metadata: ScenarioMetadata, enabled: bool | None, staff_only: bool | None, current: CredentialContext
) -> None:
    """Persist optional flags with the current principal as audit attribution."""
    if enabled is not None:
        metadata.enabled = enabled
    if staff_only is not None:
        metadata.staff_only = staff_only
    metadata.updated_by_id = None
    metadata.updated_by_principal_uuid = current.principal.uuid
    metadata.save(update_fields=["enabled", "staff_only", "updated_by", "updated_by_principal_uuid", "updated_at"])
