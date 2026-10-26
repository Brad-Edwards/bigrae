"""Organizer-owned source policies, independent of participant credentials."""

from __future__ import annotations

from ctf.exceptions import CTFValidationError
from ctf.models import CTFEvent
from ctf.services.actors import EventActor, event_actor_credential, event_actor_user_id
from shared.model_access import ContractError
from shared.model_access.sources import (
    ModelSourceCredentialReference,
    ModelSourceSelection,
    ModelSourceSponsorship,
)
from workspaces.services import OrganizationAuthorizationError, WorkspaceAuthorizationError

SPONSOR_UNAVAILABLE = "source.sponsor_unavailable"


def set_event_model_sources(
    event: CTFEvent, actor: EventActor | None, selection: object, *, expected_revision: int | None
) -> None:
    """Caller holds the event mutex; source policy changes use explicit CAS."""
    from ctf.bridges import cms_resolve_model_source_sponsorship
    from ctf.services.unified_authorization import require_event_action

    if actor is None:
        raise CTFValidationError("An organizer must authorize model sources.", code="source_sponsor_unavailable")
    credential = event_actor_credential(actor)
    require_event_action(credential, event.pk, "event.manage_config")
    if expected_revision != event.model_source_revision:
        raise CTFValidationError(
            "Model sources changed. Reload this event before saving.", code="source_revision_conflict"
        )
    try:
        selected = ModelSourceSelection.model_validate(selection)
        if selected == ModelSourceSelection.model_validate(event.model_sources):
            return
        if selected.aliases:
            if event.workspace_id is None:
                raise ContractError(SPONSOR_UNAVAILABLE)
            cms_resolve_model_source_sponsorship(credential, event.workspace_id, selected)
    except (ValueError, ContractError, WorkspaceAuthorizationError, OrganizationAuthorizationError):
        raise CTFValidationError("Selected model sources are unavailable.", code="source_unavailable") from None
    event.model_sources = selected.model_dump(mode="json")
    event.model_source_authority = (
        ModelSourceCredentialReference.from_credential(credential).model_dump(mode="json") if selected.aliases else None
    )
    event.model_source_actor_id = event_actor_user_id(actor) if selected.aliases else None
    event.model_source_revision += 1
    from shared.audit import AuditActorType, AuditEvent, audit_log

    audit_log(
        AuditEvent(
            entity_type="config",
            entity_id=0,
            entity_ref=str(event.pk),
            action="update",
            actor_type=AuditActorType.PRINCIPAL,
            actor_id=None,
            actor_principal_uuid=credential.principal.uuid,
            new_state={"event_model_source_revision": event.model_source_revision},
        ),
        strict=True,
    )


def project_event_model_sources(event: CTFEvent) -> ModelSourceSponsorship | None:
    """Revalidate the source sponsor's current event and tenant authority."""
    from ctf.bridges import cms_resolve_model_source_sponsorship
    from ctf.services.unified_authorization import require_event_action
    from shared.credentials import resolve_current_credential

    selection = ModelSourceSelection.model_validate(event.model_sources)
    if not selection.aliases:
        return None
    if event.model_source_authority is None or event.workspace_id is None:
        raise ContractError(SPONSOR_UNAVAILABLE)
    try:
        reference = ModelSourceCredentialReference.model_validate(event.model_source_authority)
        actor = resolve_current_credential(reference.credential())
    except Exception:
        raise ContractError(SPONSOR_UNAVAILABLE) from None
    require_event_action(actor, event.pk, "event.manage_config")
    return cms_resolve_model_source_sponsorship(actor, event.workspace_id, selection)
