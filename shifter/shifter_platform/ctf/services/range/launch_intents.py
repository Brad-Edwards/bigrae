"""Resume an original CTF launch across the CMS and dispatch boundaries."""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime
from uuid import UUID

from django.core.serializers.json import DjangoJSONEncoder
from django.db import transaction
from django.utils import timezone

from ctf.exceptions import CTFRangeError
from ctf.models import CTFEvent, CTFParticipant, CTFRangeLaunch, CTFRangeRecovery, CTFSpareRange
from shared.credentials import resolve_current_credential, resolve_local_credential
from shared.identity_scope import ResourceScope
from shared.model_access import OwnedReference
from shared.model_access.reservation import ModelLaunchScope
from shared.operation_authority import credential_reference_fields, credential_reference_from_record


def launch_parameters(event, owner, subject, model_scope) -> dict:
    """Freeze only public contract fields; never retain bearer proof or pack bytes."""
    from workspaces.services import resource_scope_from_ids

    scope = resource_scope_from_ids(
        kind=event.scope_kind,
        account_id=event.account_id,
        organization_id=event.organization_id,
        workspace_id=event.workspace_id,
    )
    return json.loads(
        json.dumps(
            {
                "scenario": event.scenario_id,
                "scope": asdict(scope),
                "owner_uuid": str(owner.uuid),
                "cleanup_at": event.get_cleanup_time(),
                "model_subject": subject.model_dump(mode="json"),
                "model_scope": model_scope.model_dump(mode="json") if model_scope else None,
            },
            cls=DjangoJSONEncoder,
        )
    )


def reserve_launch(event, kind: str, subject_uuid: UUID, event_admission, parameters: dict):
    """Caller holds its subject lock; commit the request before leaving SQL."""
    from ctf.services.actors import event_actor_credential
    from ctf.services.authorization import assert_event_action

    assert_event_action(event_admission, event, "event.manage_ranges")
    credential = event_actor_credential(event_admission)
    if resolve_local_credential(credential) != credential:
        raise CTFRangeError("Range launch credential changed")
    return CTFRangeLaunch.objects.create(
        event=event,
        subject_kind=kind,
        subject_uuid=subject_uuid,
        parameters=parameters,
        **credential_reference_fields(credential),
    )


def assert_launch_replay(intent, credential, parameters) -> None:
    """An idempotency key never authorizes a different actor or declaration."""
    if intent.parameters != parameters or credential_reference_from_record(intent) != credential:
        raise CTFRangeError("Range launch intent changed")


def _assert_event_binding(intent, event, owner, subject) -> None:
    """A retained request cannot follow an event into a different tenant."""
    live = launch_parameters(event, owner, subject, None)
    if (
        live | {"model_scope": None} != intent.parameters | {"model_scope": None}
        or event.deleted_at is not None
        or (intent.subject_kind == "spare" and event.owner_principal_uuid != owner.uuid)
    ):
        raise CTFRangeError("Range launch event changed")


def _locked_subject(intent, event) -> CTFParticipant | CTFSpareRange | CTFRangeRecovery:
    if intent.subject_kind == "participant":
        return CTFParticipant.objects.select_for_update().get(pk=intent.subject_uuid, event=event)
    if intent.subject_kind == "spare":
        return CTFSpareRange.objects.select_for_update().get(pk=intent.subject_uuid, event=event)
    if intent.subject_kind == "rebuild":
        return CTFRangeRecovery.objects.select_for_update().get(pk=intent.subject_uuid, event=event)
    raise CTFRangeError("Range launch subject unavailable")


def execute_launch(intent: CTFRangeLaunch) -> int:
    """Retry one immutable CMS request, bind its subject, then queue native work."""
    from ctf.bridges import (
        CTFRangeLaunchOptions,
        cms_create_range,
        cms_dispatch_bound_event_range,
        cms_find_range_instance_id,
    )
    from management.services import resolve_principal_uuid

    original = credential_reference_from_record(intent)
    credential = resolve_current_credential(original)
    if credential != original:
        raise CTFRangeError("Range launch credential changed")
    parameters = intent.parameters
    from ctf.services.authorization import admit_event_actions, assert_event_action

    event = CTFEvent.objects.get(pk=intent.event_id)
    owner = resolve_principal_uuid(UUID(parameters["owner_uuid"]))
    subject = OwnedReference.model_validate(parameters["model_subject"])
    _assert_event_binding(intent, event, owner, subject)
    event_admission = admit_event_actions(credential, event, "event.manage_ranges")
    if intent.dispatched_at is not None and intent.range_instance_id is not None:
        return intent.range_instance_id
    scope_values = parameters["scope"]
    if scope_values["kind"] not in {"account", "installation"}:
        raise CTFRangeError("Range launch scope unavailable")
    scope = ResourceScope(
        kind="account" if scope_values["kind"] == "account" else "installation",
        account_uuid=UUID(scope_values["account_uuid"]) if scope_values.get("account_uuid") else None,
        organization_uuid=UUID(scope_values["organization_uuid"]) if scope_values.get("organization_uuid") else None,
        workspace_uuid=UUID(scope_values["workspace_uuid"]) if scope_values.get("workspace_uuid") else None,
    )
    result = cms_create_range(
        scenario=parameters["scenario"],
        agents_by_os={},
        ngfw_enabled=False,
        remote_access_teardown_at=(
            datetime.fromisoformat(parameters["cleanup_at"]) if parameters["cleanup_at"] else None
        ),
        launch_options=CTFRangeLaunchOptions(
            model_admission_subject=subject,
            model_launch_scope=(
                ModelLaunchScope.model_validate(parameters["model_scope"]) if parameters["model_scope"] else None
            ),
            credential=credential,
            scope=scope,
            owner_principal=owner,
            event_uuid=intent.event_id,
            request_uuid=intent.pk,
        ),
    )
    instance_id = cms_find_range_instance_id(result.request_id)
    if instance_id is None or result.request_id != intent.pk:
        raise CTFRangeError("Range launch instance unavailable")
    with transaction.atomic():
        event = CTFEvent.objects.select_for_update().get(pk=intent.event_id)
        assert_event_action(event_admission, event, "event.manage_ranges")
        locked = CTFRangeLaunch.objects.select_for_update().get(pk=intent.pk)
        _assert_event_binding(locked, event, owner, subject)
        if resolve_local_credential(credential) != credential or event.deleted_at is not None:
            raise CTFRangeError("Range launch authority changed")
        if locked.range_instance_id not in {None, instance_id}:
            raise CTFRangeError("Range launch association changed")
        request_field, range_field = {
            "participant": ("range_request_id", "range_instance_id"),
            "spare": ("request_id", "range_instance_id"),
            "rebuild": ("replacement_request_id", "replacement_range_instance_id"),
        }[intent.subject_kind]
        row = _locked_subject(intent, event)
        if isinstance(row, CTFParticipant) and row.principal_uuid != owner.uuid:
            raise CTFRangeError("Range launch owner changed")
        if isinstance(row, CTFRangeRecovery) and row.participant.principal_uuid != owner.uuid:
            raise CTFRangeError("Range launch owner changed")
        if getattr(row, request_field) != intent.pk or getattr(row, range_field) not in {None, instance_id}:
            raise CTFRangeError("Range launch subject changed")
        setattr(row, range_field, instance_id)
        row.save(update_fields=[range_field, "updated_at"])
        locked.range_instance_id = instance_id
        locked.save(update_fields=["range_instance_id", "updated_at"])
    # A crash before or after enqueue is recoverable: the retained request and
    # incumbent Engine outbox both enforce idempotency, including original proof.
    cms_dispatch_bound_event_range(credential, intent.event_id, instance_id)
    CTFRangeLaunch.objects.filter(pk=intent.pk, dispatched_at=None).update(dispatched_at=timezone.now())
    return instance_id


def fence_launch_intent(request_id: UUID, event_uuid: UUID) -> None:
    """Serialize CMS/native creation with cancellation using CTF-owned SQL."""
    from management.services import resolve_principal_uuid

    event = CTFEvent.objects.select_for_update().get(pk=event_uuid)
    intent = CTFRangeLaunch.objects.select_for_update().get(pk=request_id, event=event)
    credential = credential_reference_from_record(intent)
    if resolve_local_credential(credential) != credential:
        raise CTFRangeError("Range launch authority changed")
    owner = resolve_principal_uuid(UUID(intent.parameters["owner_uuid"]))
    _assert_event_binding(intent, event, owner, OwnedReference.model_validate(intent.parameters["model_subject"]))
    request_field = {
        "participant": "range_request_id",
        "spare": "request_id",
        "rebuild": "replacement_request_id",
    }[intent.subject_kind]
    subject = _locked_subject(intent, event)
    if (
        getattr(subject, request_field) != intent.pk
        or (isinstance(subject, CTFParticipant) and subject.principal_uuid != owner.uuid)
        or (isinstance(subject, CTFRangeRecovery) and subject.participant.principal_uuid != owner.uuid)
    ):
        raise CTFRangeError("Range launch subject changed")


def cancel_event_launches(event_id, actor, *, action="event.manage_ranges", delete_event=False, spares_only=False):
    """Persist cancellation before discovering CMS rows that a creator may commit."""
    from ctf.services.authorization import admit_event_actions, assert_event_action

    event = CTFEvent.all_objects.get(pk=event_id)
    admission = admit_event_actions(actor, event, action)
    with transaction.atomic():
        event = CTFEvent.all_objects.select_for_update().get(pk=event_id)
        assert_event_action(admission, event, action)
        now = timezone.now()
        if delete_event and event.deleted_at is None:
            event.deleted_at = now
            event.save(update_fields=["deleted_at", "updated_at"])
        intents = CTFRangeLaunch.all_objects.filter(event=event)
        if spares_only:
            intents = intents.filter(
                subject_kind="spare",
                subject_uuid__in=CTFSpareRange.objects.filter(event=event, consumed_by__isnull=True).values("pk"),
            )
        intents.filter(deleted_at=None).update(deleted_at=now)
        return list(intents)
