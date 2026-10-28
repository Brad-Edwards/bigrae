"""Recoverable event initialization after exact relationship publication."""

from typing import TYPE_CHECKING
from uuid import UUID

if TYPE_CHECKING:
    from ctf.services.actors import EventActionAdmission

    from .model_sources import PreparedEventModelSources

from django.db import transaction
from pydantic import BaseModel, ConfigDict

from ctf.exceptions import CTFStateError, CTFValidationError
from ctf.models import CTFEvent
from ctf.services.content_resolution import ResolvedCtfContent, resolve_scenario_ctf_content
from ctf.services.credential_scope import _event_initialization_scope
from shared.credentials import CredentialContext, resolve_current_credential
from shared.model_access.sources import ModelSourceCredentialReference, ModelSourceSelection


class _InitializationIntent(BaseModel):
    """Retain the credential reference, source selection and exact content digest."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    authority: ModelSourceCredentialReference
    model_sources: ModelSourceSelection | None = None
    content_digest: str | None = None


def initialization_intent(
    credential: CredentialContext, event_data: dict[str, object], content: ResolvedCtfContent | None
) -> dict[str, object]:
    """Persist references and pinned intent, without source content or bearer secrets."""
    try:
        selection = (
            ModelSourceSelection.model_validate(event_data["model_sources"]) if "model_sources" in event_data else None
        )
        return _InitializationIntent(
            authority=ModelSourceCredentialReference.from_credential(credential),
            model_sources=selection,
            content_digest=content.evidence.declared_digest if content is not None else None,
        ).model_dump(mode="json")
    except ValueError:
        raise CTFValidationError("Event initialization is invalid") from None


def _initialization_authority(
    event: CTFEvent,
    admitted_credential: CredentialContext | None,
    recovery_credential: CredentialContext | None,
) -> tuple[_InitializationIntent, CredentialContext]:
    """Resolve stored or recovery proof and bind it to the immutable creator intent."""
    try:
        intent = _InitializationIntent.model_validate(event.initialization_intent)
        if recovery_credential is not None:
            credential = resolve_current_credential(recovery_credential)
        elif admitted_credential is not None:
            credential = admitted_credential
        else:
            credential = resolve_current_credential(intent.authority.credential())
        if (
            recovery_credential is None
            and ModelSourceCredentialReference.from_credential(credential) != intent.authority
        ):
            raise ValueError("initialization credential changed")
        if intent.authority.principal_uuid != event.owner_principal_uuid:
            raise ValueError("initialization owner changed")
    except Exception:
        raise CTFStateError("Event initialization authority is unavailable") from None
    return intent, credential


def resume_event_initialization(
    event_uuid: UUID,
    *,
    resolved_content: ResolvedCtfContent | None = None,
    _admitted_credential: CredentialContext | None = None,
    recovery_credential: CredentialContext | None = None,
    _admitted_actor: object | None = None,
) -> CTFEvent:
    """Resume the stored command under live creator or explicitly supplied recovery authority."""
    from ctf.services.unified_authorization import require_event_creation
    from shared.authorization.policy import scope_parent
    from workspaces.services import resource_scope_from_ids

    from .relationships import publish_event_relationships

    event = CTFEvent.objects.get(pk=event_uuid)
    if event.initialization_intent is None:
        return event
    intent, credential = _initialization_authority(event, _admitted_credential, recovery_credential)
    content = resolved_content or resolve_scenario_ctf_content(event.scenario_id)
    if (content.evidence.declared_digest if content is not None else None) != intent.content_digest:
        raise CTFStateError("Event initialization content changed")
    event = publish_event_relationships(event.pk)
    scope = resource_scope_from_ids(
        kind=event.scope_kind,
        account_id=event.account_id,
        organization_id=event.organization_id,
        workspace_id=event.workspace_id,
    )
    require_event_creation(_admitted_actor if _admitted_actor is not None else credential, scope_parent(scope))
    admission, sources = _prepare_initialization(
        event, intent, content, _admitted_actor if _admitted_actor is not None else credential
    )
    return _commit_initialization(event, intent, content, admission, sources)


def abort_event_initialization(event_uuid: UUID, credential: CredentialContext) -> None:
    """Remove an unusable pending declaration under current owning-parent authority."""
    from ctf.models import CTFParticipant, CTFSpareRange
    from ctf.services.unified_authorization import require_event_creation
    from shared.audit import AuditEvent, audit_log, principal_actor_fields
    from shared.authorization import AuthorizationRequest, configured_authorization_provider
    from shared.authorization.policy import scope_parent
    from shared.credentials import resolve_local_credential
    from workspaces.services import resource_scope_from_ids

    credential = resolve_current_credential(credential)
    event = CTFEvent.objects.get(pk=event_uuid)
    if event.initialization_intent is None:
        raise CTFStateError("Event initialization is already complete")
    scope = resource_scope_from_ids(
        kind=event.scope_kind,
        account_id=event.account_id,
        organization_id=event.organization_id,
        workspace_id=event.workspace_id,
    )
    parent = scope_parent(scope)
    require_event_creation(credential, parent)
    if credential.principal.uuid != event.owner_principal_uuid:
        request = AuthorizationRequest(
            credential.principal, f"{parent.type}.manage_authorization", parent, scope, credential.ceiling
        )
        if not configured_authorization_provider().check(request).allowed:
            raise CTFStateError("Event initialization abort denied")
    with transaction.atomic():
        locked = CTFEvent.objects.select_for_update().get(pk=event_uuid)
        if (
            locked.initialization_intent != event.initialization_intent
            or locked.owner_principal_uuid != event.owner_principal_uuid
            or resolve_local_credential(credential) != credential
            or CTFParticipant.all_objects.filter(event=locked).exists()
            or CTFSpareRange.all_objects.filter(event=locked).exists()
        ):
            raise CTFStateError("Event initialization cannot be aborted")
        audit_log(
            AuditEvent(
                entity_type="config",
                entity_id=0,
                entity_ref=str(locked.pk),
                action="delete",
                **principal_actor_fields(credential.principal),
                previous_state={"initializing_owner_principal_uuid": str(locked.owner_principal_uuid)},
                context="event_initialization_aborted",
            ),
            strict=True,
        )
        locked.delete(soft=False)


def _prepare_initialization(
    event: CTFEvent,
    intent: _InitializationIntent,
    content: ResolvedCtfContent | None,
    actor: object,
) -> tuple[EventActionAdmission, PreparedEventModelSources | None]:
    """Admit content and model-source work before taking the event transaction lock."""
    from ctf.services.authorization import admit_event_actions

    from .model_sources import prepare_event_model_sources

    with _event_initialization_scope(event.pk, event.owner_principal_uuid):
        actions = ["event.manage_config"]
        if content is not None:
            actions.extend(("event.manage_content", "event.manage_challenges"))
        admission = admit_event_actions(actor, event, *actions)
        sources = (
            prepare_event_model_sources(
                event, admission, intent.model_sources.model_dump(mode="json"), expected_revision=0
            )
            if intent.model_sources is not None
            else None
        )
    return admission, sources


def _commit_initialization(
    event: CTFEvent,
    intent: _InitializationIntent,
    content: ResolvedCtfContent | None,
    admission: EventActionAdmission,
    sources: PreparedEventModelSources | None,
) -> CTFEvent:
    """Fence the admitted declaration and commit content and model-source state atomically."""
    from ctf.services.content_hydration import hydrate_event_ctf_content

    from .model_sources import commit_event_model_sources

    with _event_initialization_scope(event.pk, event.owner_principal_uuid), transaction.atomic():
        locked = CTFEvent.objects.select_for_update().get(pk=event.pk)
        from ctf.services.actors import fence_event_actor

        fence_event_actor(admission)
        if locked.initialization_intent is None:
            return locked
        if (
            locked.initialization_intent != event.initialization_intent
            or locked.owner_principal_uuid != event.owner_principal_uuid
        ):
            raise CTFStateError("Event initialization changed")
        if intent.model_sources is not None:
            assert sources is not None
            commit_event_model_sources(locked, sources)
            locked.save(
                update_fields=[
                    "model_sources",
                    "model_source_authority",
                    "model_source_actor_id",
                    "model_source_revision",
                    "updated_at",
                ]
            )
        if content is not None:
            hydrate_event_ctf_content(locked.pk, content, actor_id=admission)
        locked.initialization_intent = None
        locked.save(update_fields=["initialization_intent", "updated_at"])
        return locked
