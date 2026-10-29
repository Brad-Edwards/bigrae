"""Organizer-owned source policies, independent of participant credentials."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from ctf.exceptions import CTFValidationError
from ctf.models import CTFEvent
from ctf.services.actors import EventActionAdmission, EventActor, event_actor_user_id
from shared.model_access import ContractError
from shared.model_access.sources import (
    ModelSourceCredentialReference,
    ModelSourceSelection,
    ModelSourceSponsorship,
)
from workspaces.services import OrganizationAuthorizationError, WorkspaceAuthorizationError

if TYPE_CHECKING:
    from shared.credentials import CredentialContext

_EVENT_MANAGE_CONFIG = "event.manage_config"

SPONSOR_UNAVAILABLE = "source.sponsor_unavailable"


def validate_event_model_source_selection(
    credential: CredentialContext, workspace_id: int | None, selection: object
) -> ModelSourceSelection:
    """Validate placement and sponsorship before any event declaration is committed."""
    from ctf.bridges import cms_resolve_model_source_sponsorship

    try:
        selected = ModelSourceSelection.model_validate(selection)
        if selected.aliases:
            if workspace_id is None:
                raise ContractError(SPONSOR_UNAVAILABLE)
            cms_resolve_model_source_sponsorship(credential, workspace_id, selected)
        return selected
    except (ValueError, ContractError, WorkspaceAuthorizationError, OrganizationAuthorizationError):
        raise CTFValidationError("Selected model sources are unavailable.", code="source_unavailable") from None


@dataclass(frozen=True, slots=True)
class PreparedEventModelSources:
    """Live event admission and revision expected by a source-selection commit."""

    admission: EventActionAdmission
    selection: ModelSourceSelection
    expected_revision: int | None


def prepare_event_model_sources(
    event: CTFEvent, actor: EventActor | None, selection: object, *, expected_revision: int | None
) -> PreparedEventModelSources:
    """Admit exact event and source authority outside the event transaction."""
    from ctf.services.authorization import admit_event_actions

    if actor is None:
        raise CTFValidationError("An organizer must authorize model sources.", code="source_sponsor_unavailable")
    admission = admit_event_actions(actor, event, _EVENT_MANAGE_CONFIG)
    selected = validate_event_model_source_selection(admission.credential, event.workspace_id, selection)
    return PreparedEventModelSources(admission, selected, expected_revision)


def commit_event_model_sources(event: CTFEvent, prepared: PreparedEventModelSources) -> None:
    """Fence a prepared source change under the caller's event mutex."""
    from ctf.services.authorization import assert_event_action

    assert_event_action(prepared.admission, event, _EVENT_MANAGE_CONFIG)
    if prepared.expected_revision != event.model_source_revision:
        raise CTFValidationError(
            "Model sources changed. Reload this event before saving.", code="source_revision_conflict"
        )
    selected = prepared.selection
    credential = prepared.admission.credential
    if selected == ModelSourceSelection.model_validate(event.model_sources):
        return
    event.model_sources = selected.model_dump(mode="json")
    event.model_source_authority = (
        ModelSourceCredentialReference.from_credential(credential).model_dump(mode="json") if selected.aliases else None
    )
    event.model_source_actor_id = event_actor_user_id(credential) if selected.aliases else None
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


def set_event_model_sources(
    event: CTFEvent, actor: EventActor | None, selection: object, *, expected_revision: int | None
) -> None:
    """Prepare outside SQL; aggregate callers use the explicit prepare/commit pair."""
    commit_event_model_sources(
        event, prepare_event_model_sources(event, actor, selection, expected_revision=expected_revision)
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
    except Exception:  # noqa: BLE001  # fail-closed authority boundary; suppress private causes
        raise ContractError(SPONSOR_UNAVAILABLE) from None
    require_event_action(actor, event.pk, _EVENT_MANAGE_CONFIG)
    return cms_resolve_model_source_sponsorship(actor, event.workspace_id, selection)
