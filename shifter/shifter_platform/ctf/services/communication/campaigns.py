"""Campaign authoring service for scoped communications (ADR-051, #2048).

Campaigns use one explicit account, organization, or workspace parent. Every
requested target must share that exact placement and admit current native
``event.manage_communications`` policy within the credential ceiling. Content
uses the safe profile, and edits append immutable revisions. SQL membership,
creator attribution, and legacy staff flags grant no application authority.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

from django.conf import settings
from django.db import transaction

import workspaces.services as workspace_services
from ctf.communication_contracts import (
    validate_acknowledgement_policy,
    validate_audience_spec,
    validate_campaign_targets,
    validate_channels,
    validate_message_content,
    validate_trigger_spec,
)
from ctf.enums_communication import CampaignStatus
from ctf.exceptions import CTFCommunicationError
from ctf.models import CommunicationCampaign, CommunicationTargetEvent, CTFEvent, MessageRevision
from ctf.services.actors import EventActor, event_actor_credential, event_actor_user, event_actor_user_id
from ctf.services.communication.admission import (
    AdmissionActor,
    lock_authorized_campaign,
    prepare_admission,
    prepare_campaign_admission,
    reauthorize,
)
from ctf.services.communication.attribution import actor_attribution
from ctf.services.communication.backpressure import enforce_operation_rate
from ctf.services.unified_authorization import require_event_action
from shared.identity_scope import ResourceScope
from workspaces.services import ResolvedResourceScope

_CAMPAIGN_SCOPE_IS_UNAVAILABLE = "Campaign scope is unavailable"

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class CampaignDraft:
    """The organizer-supplied shape of a new campaign (ADR-051, #2048).

    A value object so campaign authoring is one bounded input, not a long
    positional/keyword list. The service validates every field before persisting.
    """

    title: str
    origin: str
    target_event_ids: list[UUID]
    audience_spec: dict[str, Any]
    trigger_spec: dict[str, Any]
    channels: list[str]
    subject: str
    body: str
    acknowledgement_policy: str = "none"
    actor_token_id: int | None = None


_TARGET_DENIED = CTFCommunicationError(
    "One or more target events are not available for this campaign",
    code="CTF_COMMUNICATION_TARGET_DENIED",
)


def _resolve_parent(parent_type: str, parent_uuid: str | UUID) -> tuple[ResourceScope, ResolvedResourceScope]:
    """Resolve one type-correct event parent; event policy supplies permission."""
    try:
        scope = workspace_services.event_placement_scope(parent_type, UUID(str(parent_uuid)))
        binding = workspace_services.resolve_resource_scope(scope)
    except (ValueError, workspace_services.AccountScopeError) as exc:
        raise CTFCommunicationError(
            "Scope is not available for CTF communications",
            code="CTF_COMMUNICATION_WORKSPACE_DENIED",
        ) from exc
    return scope, binding


def _legacy_workspace_id(campaign: CommunicationCampaign) -> int:
    """Reject campaign scopes unsupported by the workspace communication service."""
    if campaign.workspace_id is None:
        raise CTFCommunicationError(
            "Campaign is not available for workspace communications",
            code="CTF_COMMUNICATION_WORKSPACE_DENIED",
        )
    return campaign.workspace_id


def campaign_rate_scope(campaign: CommunicationCampaign) -> tuple[str, int]:
    """Return the narrowest durable campaign scope used by admission budgets."""
    if campaign.workspace_id is not None:
        return "workspace", campaign.workspace_id
    if campaign.account_id is not None:
        return "account", campaign.account_id
    raise CTFCommunicationError(_CAMPAIGN_SCOPE_IS_UNAVAILABLE, code="CTF_COMMUNICATION_WORKSPACE_DENIED")


def _authorized_target_events(
    credential: EventActor, scope: ResourceScope, binding: ResolvedResourceScope, target_event_ids: list[UUID]
) -> list[CTFEvent]:
    """Return only exact-scope events admitted by current native communication policy.

    Every requested event must exist, carry the campaign's exact scope, and
    admit the actor's ``event.manage_communications`` action. Any failure raises one opaque
    denial (no partial success, no oracle).
    """
    if not target_event_ids:
        raise CTFCommunicationError("A campaign must target at least one event", code="CTF_COMMUNICATION_NO_TARGETS")
    unique_ids = list(dict.fromkeys(target_event_ids))
    events = {event.id: event for event in CTFEvent.objects.filter(id__in=unique_ids).order_by("pk")}
    resolved: list[CTFEvent] = []
    for event_id in unique_ids:
        event = events.get(event_id)
        if event is None or (
            event.scope_kind,
            event.account_id,
            event.organization_id,
            event.workspace_id,
        ) != (scope.kind, binding.account_id, binding.organization_id, binding.workspace_id):
            raise _TARGET_DENIED
        try:
            require_event_action(credential, event.pk, "event.manage_communications")
        except Exception:  # noqa: BLE001  # fail-closed authority boundary; suppress private causes
            raise _TARGET_DENIED from None
        resolved.append(event)
    return resolved


def create_campaign(
    principal_actor: EventActor,
    parent_uuid: str | UUID,
    draft: CampaignDraft,
    *,
    actor: AdmissionActor | None = None,
    parent_type: str = "workspace",
    campaign_id: UUID | None = None,
    maintenance: bool = False,
) -> CommunicationCampaign:
    """Create an exactly scoped draft campaign with an initial immutable revision."""
    credential = event_actor_credential(principal_actor)
    from shared.credentials import CredentialContext

    actor = actor or AdmissionActor(
        credential=credential,
        compatibility_user_id=(
            None if isinstance(principal_actor, CredentialContext) else event_actor_user_id(principal_actor)
        ),
    )
    if actor.credential != credential or actor.system:
        raise CTFCommunicationError("Unavailable", code="CTF_COMMUNICATION_ACTOR_DENIED")
    scope, binding = _resolve_parent(parent_type, parent_uuid)
    workspace_id = binding.workspace_id

    audience = validate_audience_spec(draft.audience_spec)
    trigger = validate_trigger_spec(draft.trigger_spec)
    selected_channels = validate_channels(draft.channels)
    ack_policy = validate_acknowledgement_policy(draft.acknowledgement_policy)
    content = validate_message_content(
        {"subject": draft.subject, "body": draft.body},
        allowed_link_hosts=settings.CTF_COMMUNICATION_ALLOWED_LINK_HOSTS,
    )

    events = _authorized_target_events(
        principal_actor, scope, binding, validate_campaign_targets(draft.target_event_ids)
    )
    prospective = CommunicationCampaign(
        scope_kind=scope.kind,
        account_id=binding.account_id,
        organization_id=binding.organization_id,
        workspace_id=workspace_id,
    )
    actor = prepare_admission(prospective, events, actor)
    with transaction.atomic():
        events = list(CTFEvent.objects.select_for_update().filter(pk__in=[event.pk for event in events]).order_by("pk"))
        _enforce_creation_rate(credential.principal.uuid, binding, maintenance)
        actor = reauthorize(prospective, events, actor, operation="create")
        attribution = actor_attribution(actor)
        campaign = CommunicationCampaign.objects.create(
            id=campaign_id or uuid4(),
            workspace_id=workspace_id,
            scope_kind=scope.kind,
            account_id=binding.account_id,
            organization_id=binding.organization_id,
            title=draft.title,
            origin=draft.origin,
            created_by=event_actor_user(principal_actor),
            creator_principal_uuid=credential.principal.uuid,
            actor_token_id=attribution.token_id,
            status=CampaignStatus.DRAFT.value,
            audience_spec=audience,
            trigger_spec=trigger,
            channels=selected_channels,
            acknowledgement_policy=ack_policy,
        )
        CommunicationTargetEvent.objects.bulk_create(
            [CommunicationTargetEvent(campaign=campaign, event=event) for event in events]
        )
        MessageRevision.objects.create(
            campaign=campaign,
            revision_number=1,
            subject=content["subject"],
            body=content["body"],
            content_profile=content["profile"],
            content_digest=content["digest"],
            actor_user_id=attribution.user_id,
            actor_token_id=attribution.token_id,
            actor_principal_uuid=attribution.principal_uuid,
        )
    logger.info("Created communication campaign %s in workspace %s", campaign.id, workspace_id)
    return campaign


def create_system_event_campaign(event: CTFEvent, draft: CampaignDraft, *, campaign_id: UUID) -> CommunicationCampaign:
    """Create one trusted lifecycle notice bound to an already-locked event."""
    if list(dict.fromkeys(validate_campaign_targets(draft.target_event_ids))) != [event.pk]:
        raise _TARGET_DENIED
    try:
        scope = workspace_services.resource_scope_from_ids(
            kind=event.scope_kind,
            account_id=event.account_id,
            organization_id=event.organization_id,
            workspace_id=event.workspace_id,
        )
        binding = workspace_services.resolve_resource_scope(scope)
    except workspace_services.AccountScopeError:
        raise CTFCommunicationError(_CAMPAIGN_SCOPE_IS_UNAVAILABLE, code="CTF_COMMUNICATION_WORKSPACE_DENIED") from None
    audience = validate_audience_spec(draft.audience_spec)
    trigger = validate_trigger_spec(draft.trigger_spec)
    selected_channels = validate_channels(draft.channels)
    ack_policy = validate_acknowledgement_policy(draft.acknowledgement_policy)
    content = validate_message_content(
        {"subject": draft.subject, "body": draft.body},
        allowed_link_hosts=settings.CTF_COMMUNICATION_ALLOWED_LINK_HOSTS,
    )
    scope_kind = "workspace" if binding.workspace_id is not None else "account"
    scope_id = binding.workspace_id or binding.account_id
    if scope_id is None:
        raise CTFCommunicationError(_CAMPAIGN_SCOPE_IS_UNAVAILABLE, code="CTF_COMMUNICATION_WORKSPACE_DENIED")
    enforce_operation_rate("system", scope_kind, scope_id)
    campaign = CommunicationCampaign.objects.create(
        id=campaign_id,
        scope_kind=scope.kind,
        account_id=binding.account_id,
        organization_id=binding.organization_id,
        workspace_id=binding.workspace_id,
        title=draft.title,
        origin=draft.origin,
        created_by=event.created_by,
        creator_principal_uuid=event.owner_principal_uuid,
        status=CampaignStatus.DRAFT.value,
        audience_spec=audience,
        trigger_spec=trigger,
        channels=selected_channels,
        acknowledgement_policy=ack_policy,
    )
    CommunicationTargetEvent.objects.create(campaign=campaign, event=event)
    MessageRevision.objects.create(
        campaign=campaign,
        revision_number=1,
        subject=content["subject"],
        body=content["body"],
        content_profile=content["profile"],
        content_digest=content["digest"],
    )
    return campaign


def revise_message(
    campaign: CommunicationCampaign, *, subject: str, body: str, actor: AdmissionActor
) -> MessageRevision:
    """Create the next immutable message revision for a draft campaign.

    Editing content never mutates an existing revision (AC4); a new revision is
    appended. Only a draft campaign may be revised.
    """
    content = validate_message_content(
        {"subject": subject, "body": body},
        allowed_link_hosts=settings.CTF_COMMUNICATION_ALLOWED_LINK_HOSTS,
    )
    actor = prepare_campaign_admission(campaign, actor)
    with transaction.atomic():
        campaign = lock_authorized_campaign(campaign, actor, operation="revise")
        attribution = actor_attribution(actor)
        if campaign.status != CampaignStatus.DRAFT.value:
            raise CTFCommunicationError(
                "Only a draft campaign's message can be revised",
                code="CTF_COMMUNICATION_NOT_DRAFT",
            )
        actor_key = actor.credential.principal.uuid if actor.credential is not None else actor.user_id
        scope_kind, scope_id = campaign_rate_scope(campaign)
        enforce_operation_rate(actor_key, scope_kind, scope_id)
        next_number = (
            MessageRevision.objects.filter(campaign=campaign)
            .order_by("-revision_number")
            .values_list("revision_number", flat=True)
            .first()
            or 0
        ) + 1
        return MessageRevision.objects.create(
            campaign=campaign,
            revision_number=next_number,
            subject=content["subject"],
            body=content["body"],
            content_profile=content["profile"],
            content_digest=content["digest"],
            actor_user_id=attribution.user_id,
            actor_token_id=attribution.token_id,
            actor_principal_uuid=attribution.principal_uuid,
        )


def _enforce_creation_rate(principal_uuid: UUID, binding: ResolvedResourceScope, maintenance: bool) -> None:
    """Require the cutover fence or enforce the owning scope's ordinary rate limit."""
    workspace_id = binding.workspace_id
    if maintenance:
        from ctf.models import CommunicationCutover

        if not CommunicationCutover.objects.filter(pk=1, activated_at__isnull=True).exists():
            raise CTFCommunicationError("Maintenance fence required", code="CTF_COMMUNICATION_CUTOVER_BLOCKED")
    else:
        rate_kind = "workspace" if workspace_id is not None else "account"
        rate_id = workspace_id or binding.account_id
        if rate_id is None:
            raise CTFCommunicationError(_CAMPAIGN_SCOPE_IS_UNAVAILABLE, code="CTF_COMMUNICATION_WORKSPACE_DENIED")
        enforce_operation_rate(principal_uuid, rate_kind, rate_id)
