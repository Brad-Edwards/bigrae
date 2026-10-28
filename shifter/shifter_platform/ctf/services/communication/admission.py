"""Live re-authorization for the one communication admission transaction (#2099).

Every trigger source normalizes a bounded declaration and then enters this single
admission path. Admission resolves current native credentials and event policy
before SQL locks. The locked release transaction then fences the exact admitted
credential, campaign placement, and complete event set with local reads. Origin,
actor attribution, and scheduler metadata never supply authority (ADR-051-R12).

Token actors require the original live owner/token pair and the exact communication
scope; neither event scopes nor system authority substitute for that identity.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from datetime import datetime
from typing import TYPE_CHECKING, Any

from django.contrib.auth import get_user_model

from ctf.enums_communication import TriggerKind
from ctf.exceptions import CTFCommunicationError, CTFError, CTFPermissionError
from ctf.services.actors import EventActionAdmission
from shared.api_tokens.models import ApiToken
from shared.api_tokens.scopes import CTF_COMMUNICATION_WRITE, has_scope

if TYPE_CHECKING:
    from django.contrib.auth.models import User

    from ctf.models import CommunicationCampaign, CTFEvent
    from shared.credentials import CredentialContext

logger = logging.getLogger(__name__)

# Trigger sources whose realization exists in this slice: an organizer session
# (manual), an organizer-authored absolute UTC schedule, and a trusted system
# milestone (event lifecycle). A new source is added at this validated seam.
SUPPORTED_TRIGGER_KINDS = frozenset(
    {TriggerKind.MANUAL.value, TriggerKind.EVENT_LIFECYCLE.value, TriggerKind.ABSOLUTE_TIME.value}
)
# Sources whose realization consumes a RAES interpreter / CMS generation projection
# that is provisioning-only at this baseline. Per ADR-051-R12 unsupported
# realization stays CLOSED (fail-closed) rather than being approximated as an
# absolute timestamp or a range-instance id; they are enabled by later slices.
CLOSED_TRIGGER_KINDS = frozenset({TriggerKind.RAES_OCCURRENCE.value, TriggerKind.RANGE_SIGNAL.value})


def assert_source_realizable(trigger_kind: str) -> None:
    """Fail-closed for a trigger source whose realization is not available yet.

    Called at the head of every admission entry (release and schedule) so an
    unsupported source admits no work for any caller, including replay.
    """
    if trigger_kind in CLOSED_TRIGGER_KINDS:
        raise CTFCommunicationError(
            "This communication trigger source is not available yet",
            code="CTF_COMMUNICATION_SOURCE_CLOSED",
        )
    if trigger_kind not in SUPPORTED_TRIGGER_KINDS:
        raise CTFCommunicationError("Unknown communication trigger source", code="CTF_COMMUNICATION_SOURCE_UNKNOWN")


def _parse_trigger_due(trigger: dict[str, Any]) -> datetime:
    """Parse a trigger's already-normalized UTC ``due_at`` into an aware datetime."""
    return datetime.fromisoformat(trigger["due_at"])


def assert_occurrence_ready(
    campaign: CommunicationCampaign,
    target_events: list[CTFEvent],
    *,
    now: datetime,
    immediate: bool,
    due_at: datetime | None = None,
    allow_early: bool = False,
) -> None:
    """Enforce the source-specific occurrence condition at the admission boundary.

    ``assert_source_realizable`` only proves the trigger *kind* is supported; this
    proves the declared *occurrence* has actually happened before any audience is
    materialized (#2099), so a future absolute-time campaign cannot be released
    immediately, a scheduled due time cannot diverge from the campaign's authored
    trigger, and a lifecycle campaign cannot release before its milestone.

    ``immediate`` is True for a release-now path and False for a schedule path.
    ``allow_early`` (authorized organizer run-now) waives the not-yet-due check.
    """
    trigger = campaign.trigger_spec
    kind = trigger.get("kind")
    if kind == TriggerKind.MANUAL.value:
        # the occurrence is the manual action itself
        return
    if kind == TriggerKind.ABSOLUTE_TIME.value:
        trigger_due = _parse_trigger_due(trigger)
        if immediate:
            if not allow_early and now < trigger_due:
                raise CTFCommunicationError(
                    "This timed communication is not yet due; schedule it instead",
                    code="CTF_COMMUNICATION_NOT_DUE",
                )
        elif due_at is None or due_at != trigger_due:
            raise CTFCommunicationError(
                "Scheduled due time does not match the campaign's absolute-time trigger",
                code="CTF_COMMUNICATION_DUE_MISMATCH",
            )
        return
    if kind == TriggerKind.EVENT_LIFECYCLE.value:
        if not immediate:
            raise CTFCommunicationError(
                "An event-lifecycle communication fires on its milestone, not a clock schedule",
                code="CTF_COMMUNICATION_LIFECYCLE_NOT_SCHEDULABLE",
            )
        declared = trigger.get("event_status")
        if any(event.status != declared for event in target_events):
            raise CTFCommunicationError(
                "The event-lifecycle milestone has not occurred on every target event",
                code="CTF_COMMUNICATION_MILESTONE_NOT_REACHED",
            )
    # Closed kinds are already rejected by assert_source_realizable.


@dataclass(frozen=True, slots=True)
class AdmissionActor:
    """Who is admitting a communication, proven server-side by the caller.

    HTTP and durable native callers supply ``credential``. Its owning service
    rechecks lifecycle and narrowing before current event policy is admitted.
    Legacy token references retain the exact original live owner/token pair;
    trusted synchronous ``user_id`` callers enter the explicit human compatibility
    path and still require native policy. ``system`` is reserved for proven
    internal lifecycle automation; a missing actor never becomes system authority.

    ``allow_early_release`` authorizes releasing a scheduled declaration before its
    due time (organizer run-now); it is honored only on the live-session path.
    """

    user_id: int | None = None
    token_id: int | None = None
    system: bool = False
    allow_early_release: bool = False
    request_id: str = ""
    credential: CredentialContext | None = None
    event_admissions: tuple[EventActionAdmission, ...] = ()
    campaign_binding: tuple[object, ...] = ()
    compatibility_user_id: int | None = None


def _live_actor(user_id: int) -> User:
    """Return the active user row for ``user_id`` or deny (revoked/inactive/absent)."""
    user = get_user_model().objects.filter(pk=user_id, is_active=True).first()
    profile = getattr(user, "profile", None)
    if user is None or getattr(profile, "deleted_at", None) or getattr(profile, "is_ctf_account", False):
        raise CTFCommunicationError("Communication actor is not active", code="CTF_COMMUNICATION_ACTOR_DENIED")
    return user


def _assert_target_set(campaign: CommunicationCampaign, target_events: list[CTFEvent]) -> None:
    """Reject empty, deleted, cross-scope or incomplete target sets."""
    if not target_events or any(
        event.deleted_at is not None
        or event.scope_kind != campaign.scope_kind
        or event.account_id != campaign.account_id
        or event.organization_id != campaign.organization_id
        or event.workspace_id != campaign.workspace_id
        for event in target_events
    ):
        raise CTFCommunicationError("Unavailable", code="CTF_COMMUNICATION_TARGET_DENIED")
    if not campaign._state.adding and len(target_events) != campaign.target_event_links.count():
        raise CTFCommunicationError("Unavailable", code="CTF_COMMUNICATION_TARGET_DENIED")


def _authorize_token(actor: AdmissionActor, required_scope: str) -> CredentialContext | None:
    """Lock the original token and return its current canonical credential."""
    if actor.token_id is not None:
        token = ApiToken.objects.filter(pk=actor.token_id).first()
        if (
            token is None
            or actor.user_id is None
            or token.created_by_id != actor.user_id
            or not token.is_active
            or not token.has_eligible_owner
            or not has_scope(token.scopes, required_scope)
            or actor.system
            or actor.allow_early_release
        ):
            raise CTFCommunicationError("Communication token is not authorized", code="CTF_COMMUNICATION_TOKEN_DENIED")
        try:
            from shared.api_tokens.policy import personal_credential_for_token

            credential = personal_credential_for_token(token)
        except Exception as exc:
            from shared.credentials import CredentialUnavailable

            if isinstance(exc, CredentialUnavailable):
                raise CTFCommunicationError(
                    "Communication authority unavailable", code="CTF_COMMUNICATION_AUTHORITY_UNAVAILABLE"
                ) from None
            raise CTFCommunicationError(
                "Communication token is not authorized", code="CTF_COMMUNICATION_TOKEN_DENIED"
            ) from None
        if actor.credential is not None and (
            actor.credential.principal != credential.principal
            or actor.credential.credential_uuid != credential.credential_uuid
        ):
            raise CTFCommunicationError("Communication token is not authorized", code="CTF_COMMUNICATION_TOKEN_DENIED")
        return credential
    return None


def _canonical_actor(actor: AdmissionActor, required_scope: str) -> AdmissionActor:
    """Convert trusted compatibility inputs to the one credential contract."""
    from shared.credentials import resolve_current_credential

    if actor.compatibility_user_id is not None:
        from ctf.services.actors import event_actor_credential

        compatibility = event_actor_credential(_live_actor(actor.compatibility_user_id))
        if actor.token_id is not None or actor.system or actor.credential != compatibility:
            raise CTFCommunicationError("Unavailable", code="CTF_COMMUNICATION_ACTOR_DENIED")
        return actor
    if actor.credential is not None:
        actor = replace(actor, credential=resolve_current_credential(actor.credential))
    token_credential = _authorize_token(actor, required_scope)
    if token_credential is not None:
        return replace(actor, credential=actor.credential or token_credential)
    if actor.credential is not None:
        if actor.credential.kind in {"personal", "service"} and not has_scope(actor.credential.scopes, required_scope):
            raise CTFCommunicationError(
                "Communication credential is not authorized", code="CTF_COMMUNICATION_TOKEN_DENIED"
            )
        return actor
    if actor.system:
        return actor
    if actor.user_id is None:
        raise CTFCommunicationError("A communication actor is required", code="CTF_COMMUNICATION_ACTOR_REQUIRED")
    from ctf.services.actors import event_actor_credential

    return replace(
        actor, credential=event_actor_credential(_live_actor(actor.user_id)), compatibility_user_id=actor.user_id
    )


def _campaign_binding(campaign) -> tuple[object, ...]:
    return tuple(
        getattr(campaign, name) for name in ("pk", "scope_kind", "account_id", "organization_id", "workspace_id")
    )


def prepare_collection_actor(actor: AdmissionActor, *, required_scope: str) -> AdmissionActor:
    """Retain credential failures even when every collection candidate is ineligible."""
    from shared.credentials import CredentialRevoked

    try:
        return _canonical_actor(actor, required_scope)
    except CTFCommunicationError:
        raise
    except ValueError as exc:
        code = (
            "CTF_COMMUNICATION_ACTOR_DENIED"
            if isinstance(exc, CredentialRevoked)
            else "CTF_COMMUNICATION_AUTHORITY_UNAVAILABLE"
        )
        raise CTFCommunicationError("Communication credential unavailable", code=code) from None


def prepare_admission(campaign, target_events, actor, *, required_scope=CTF_COMMUNICATION_WRITE):
    """Resolve live policy before opening the bounded aggregate transaction."""
    from ctf.services.authorization import admit_event_actions

    _assert_target_set(campaign, target_events)
    try:
        actor = _canonical_actor(actor, required_scope)
        admissions = (
            tuple(
                admit_event_actions(
                    actor.compatibility_user_id if actor.compatibility_user_id is not None else actor.credential,
                    event,
                    "event.manage_communications",
                )
                for event in target_events
            )
            if actor.credential is not None
            else ()
        )
    except CTFCommunicationError:
        raise
    except CTFPermissionError:
        raise CTFCommunicationError("Communication authority denied", code="CTF_COMMUNICATION_EVENT_DENIED") from None
    except CTFError:
        raise CTFCommunicationError(
            "Communication authority unavailable", code="CTF_COMMUNICATION_AUTHORITY_UNAVAILABLE"
        ) from None
    except ValueError as exc:
        from shared.credentials import CredentialRevoked

        code = (
            "CTF_COMMUNICATION_ACTOR_DENIED"
            if isinstance(exc, CredentialRevoked)
            else "CTF_COMMUNICATION_AUTHORITY_UNAVAILABLE"
        )
        raise CTFCommunicationError("Communication credential unavailable", code=code) from None
    return replace(actor, event_admissions=admissions, campaign_binding=_campaign_binding(campaign))


def prepare_campaign_admission(campaign, actor):
    """Read the immutable target set and admit it before campaign locks."""
    return prepare_admission(campaign, list(campaign.target_events.order_by("pk")), actor)


def _fence_local_actor(actor: AdmissionActor, required_scope: str) -> None:
    """Translate current local proof failures at the SQL fence into bounded domain errors."""
    from shared.credentials import resolve_local_credential

    if actor.credential is not None:
        if actor.compatibility_user_id is None:
            from shared.credentials import CredentialUnavailable

            try:
                current = resolve_local_credential(actor.credential)
            except CredentialUnavailable:
                raise CTFCommunicationError(
                    "Communication authority unavailable", code="CTF_COMMUNICATION_AUTHORITY_UNAVAILABLE"
                ) from None
            except ValueError:
                raise CTFCommunicationError(
                    "Communication credential changed", code="CTF_COMMUNICATION_ACTOR_DENIED"
                ) from None
            if current != actor.credential:
                raise CTFCommunicationError("Communication credential changed", code="CTF_COMMUNICATION_ACTOR_DENIED")
        if actor.token_id is not None:
            token = ApiToken.objects.select_for_update().filter(pk=actor.token_id).first()
            if (
                token is None
                or not token.is_active
                or not token.has_eligible_owner
                or not has_scope(token.scopes, required_scope)
            ):
                raise CTFCommunicationError("Communication token changed", code="CTF_COMMUNICATION_TOKEN_DENIED")


def reauthorize(
    campaign: CommunicationCampaign,
    target_events: list[CTFEvent],
    actor: AdmissionActor,
    *,
    required_scope: str = CTF_COMMUNICATION_WRITE,
    audit_authority: bool = True,
    operation: str = "admit",
) -> AdmissionActor:
    """Fence admitted credentials and exact ancestry under the aggregate locks."""
    _assert_target_set(campaign, target_events)
    if actor.campaign_binding != _campaign_binding(campaign):
        raise CTFCommunicationError("Communication admission changed", code="CTF_COMMUNICATION_EVENT_DENIED")
    _fence_local_actor(actor, required_scope)
    if actor.credential is not None:
        from ctf.services.authorization import assert_event_action
        from ctf.services.communication.audit import audit_communication_authority

        try:
            current = actor.credential
            admissions = {admission.event_uuid: admission for admission in actor.event_admissions}
            if set(admissions) != {event.pk for event in target_events}:
                raise CTFPermissionError("Communication admission changed")
            for event in target_events:
                assert_event_action(admissions[event.pk], event, "event.manage_communications")
                if audit_authority and required_scope == CTF_COMMUNICATION_WRITE:
                    audit_communication_authority(
                        event_id=event.pk,
                        actor_id=current,
                        token_id=None,
                        authority_source="policy",
                        request_id=actor.request_id,
                        operation=operation,
                    )
        except CTFCommunicationError:
            raise
        except CTFPermissionError:
            raise CTFCommunicationError(
                "Actor lacks communication authority on a target event",
                code="CTF_COMMUNICATION_EVENT_DENIED",
            ) from None
        except CTFError:
            raise CTFCommunicationError(
                "Communication authority unavailable", code="CTF_COMMUNICATION_AUTHORITY_UNAVAILABLE"
            ) from None
        return actor
    if actor.system and actor.token_id is None:
        return actor
    raise CTFCommunicationError("Communication authority unavailable", code="CTF_COMMUNICATION_AUTHORITY_UNAVAILABLE")


def lock_authorized_campaign(
    campaign: CommunicationCampaign, actor: AdmissionActor, *, operation: str = "update"
) -> CommunicationCampaign:
    """Serialize every authoring mutation in the release/lifecycle lock order."""
    from ctf.models import CommunicationCampaign, CTFEvent

    events = list(
        CTFEvent.objects.select_for_update().filter(communication_target_links__campaign=campaign).order_by("pk")
    )
    locked = CommunicationCampaign.objects.select_for_update().get(pk=campaign.pk)
    reauthorize(locked, events, actor, operation=operation)
    return locked


def admit_session_early_release(actor: AdmissionActor) -> AdmissionActor:
    """Session-only admission shared by run-now and the public due-release command."""
    if actor.credential is None or actor.token_id is not None or actor.system or not actor.allow_early_release:
        raise CTFCommunicationError(
            "Early release of a scheduled communication is not authorized",
            code="CTF_COMMUNICATION_EARLY_RELEASE_DENIED",
        )
    from dataclasses import replace

    from shared.credentials import CredentialUnavailable, resolve_current_credential

    try:
        current = resolve_current_credential(actor.credential)
    except CredentialUnavailable:
        raise CTFCommunicationError(
            "Communication authority unavailable", code="CTF_COMMUNICATION_AUTHORITY_UNAVAILABLE"
        ) from None
    except ValueError:
        raise CTFCommunicationError("Communication actor denied", code="CTF_COMMUNICATION_ACTOR_DENIED") from None
    if current.kind != "session":
        raise CTFCommunicationError(
            "Early release of a scheduled communication is not authorized",
            code="CTF_COMMUNICATION_EARLY_RELEASE_DENIED",
        )
    return replace(actor, credential=current)
