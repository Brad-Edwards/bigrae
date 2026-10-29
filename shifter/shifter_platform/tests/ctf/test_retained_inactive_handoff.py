"""Exact retained handoffs settle without granting a new inactive recipient."""

from dataclasses import replace
from uuid import uuid4

import pytest

from ctf.exceptions import CTFStateError, CTFValidationError
from ctf.models import CTFEventAuthorityTransition
from ctf.services.event import assign_event_staff, transfer_event_ownership
from shared.authorization import AuthorizationProviderError, PolicyEffect, configured_authorization_provider
from tests.ctf.test_event_staff_api import helper_organizer as helper_organizer

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize("failure", ["before_recipient_admission", "recipient_write", "recipient_readback"])
@pytest.mark.parametrize("recipient_kind", ["human", "service"])
def test_inactive_recipient_handoff_settles_and_can_transfer_onward(
    ctf_event,
    organizer_credential,
    helper_organizer,
    second_organizer_user,
    admin_user,
    live_session_credential,
    monkeypatch,
    failure,
    recipient_kind,
):
    if recipient_kind == "service":
        from management.services import create_service_principal

        recipient = create_service_principal("Synthetic handoff recipient").uuid
    else:
        recipient = helper_organizer.identity_principal.uuid
    assign_event_staff(ctf_event.pk, organizer_credential, recipient, "co_organizer")
    provider = configured_authorization_provider()
    write, read = provider.write_relationships, provider.read_relationships
    failed = False

    def is_recipient_grant(change):
        return change.effect == PolicyEffect.GRANT and change.grant_tuple.relation == "direct_administrator"

    def interrupted_write(change):
        nonlocal failed
        if not failed and (
            (failure == "before_recipient_admission" and not is_recipient_grant(change))
            or (failure == "recipient_write" and is_recipient_grant(change))
        ):
            failed = True
            raise AuthorizationProviderError("Synthetic handoff write outage")
        return write(change)

    def interrupted_read(change):
        nonlocal failed
        if not failed and failure == "recipient_readback" and is_recipient_grant(change):
            failed = True
            raise AuthorizationProviderError("Synthetic handoff acknowledgement loss")
        return read(change)

    monkeypatch.setattr(provider, "write_relationships", interrupted_write)
    monkeypatch.setattr(provider, "read_relationships", interrupted_read)
    with pytest.raises(CTFStateError):
        transfer_event_ownership(ctf_event.pk, organizer_credential, recipient)
    transition = CTFEventAuthorityTransition.objects.get(event=ctf_event, state="pending")
    if failure == "before_recipient_admission":
        from workspaces.models import AuthorizationOperation

        assert not AuthorizationOperation.objects.filter(
            idempotency_key=f"event-owner-{transition.pk.hex}-grant-target"
        ).exists()
    if recipient_kind == "service":
        from management.models import Principal

        Principal.objects.filter(uuid=recipient).update(is_active=False)
    else:
        helper_organizer.is_active = False
        helper_organizer.save(update_fields=["is_active"])
    monkeypatch.setattr(provider, "write_relationships", write)
    monkeypatch.setattr(provider, "read_relationships", read)
    administrator = live_session_credential(admin_user)

    result = transfer_event_ownership(ctf_event.pk, administrator, recipient)
    assert result.owner_principal_uuid == recipient
    transition.refresh_from_db()
    assert transition.state == "confirmed"
    assert transition.actor_credential_uuid == organizer_credential.credential_uuid
    # Settling the transition must also restore usable administration when its
    # canonical owner is inactive, including the former-owner staff effects.
    onward = second_organizer_user.identity_principal.uuid
    assign_event_staff(ctf_event.pk, administrator, onward, "co_organizer")
    result = transfer_event_ownership(ctf_event.pk, administrator, onward)
    assert result.owner_principal_uuid == onward
    assert not CTFEventAuthorityTransition.objects.filter(event=ctf_event, state="pending").exists()


def test_new_handoff_to_inactive_recipient_is_denied(ctf_event, organizer_credential, helper_organizer):
    recipient = helper_organizer.identity_principal.uuid
    assign_event_staff(ctf_event.pk, organizer_credential, recipient, "co_organizer")
    helper_organizer.is_active = False
    helper_organizer.save(update_fields=["is_active"])
    with pytest.raises(CTFValidationError) as error:
        transfer_event_ownership(ctf_event.pk, organizer_credential, recipient)
    assert error.value.code == "CTF_TRANSFER_TARGET_INELIGIBLE"
    assert not CTFEventAuthorityTransition.objects.filter(event=ctf_event, kind="transfer").exists()


@pytest.mark.parametrize("defect", ["unbound", "wrong_previous_owner", "wrong_recipient", "confirmed"])
def test_retained_transfer_contract_rejects_unbound_or_changed_intent(
    ctf_event,
    organizer_credential,
    helper_organizer,
    monkeypatch,
    defect,
):
    from shared.authorization import AdministrativeRoleChange, RelationshipSubject, TargetRef
    from shared.authorization.inventory import resolve_event_authority_plan
    from workspaces.services import (
        NativeRelationshipMutationRequest,
        RetainedEventOwnershipTransferMutationRequest,
        apply_native_relationship_mutation,
    )

    recipient = helper_organizer.identity_principal.uuid
    assign_event_staff(ctf_event.pk, organizer_credential, recipient, "co_organizer")
    provider = configured_authorization_provider()
    write = provider.write_relationships

    def unavailable(change):
        raise AuthorizationProviderError("Synthetic provider outage")

    monkeypatch.setattr(provider, "write_relationships", unavailable)
    with pytest.raises(CTFStateError):
        transfer_event_ownership(ctf_event.pk, organizer_credential, recipient)
    transition = CTFEventAuthorityTransition.objects.get(event=ctf_event, state="pending")
    transfer = resolve_event_authority_plan(transition.pk)
    if defect == "unbound":
        transfer = replace(transfer, transition_uuid=uuid4())
    elif defect == "wrong_previous_owner":
        transfer = replace(transfer, previous_owner_uuid=uuid4())
    elif defect == "wrong_recipient":
        transfer = replace(transfer, target_uuid=uuid4())
    else:
        CTFEventAuthorityTransition.objects.filter(pk=transition.pk).update(state="confirmed")
    calls = []

    def observed(change):
        calls.append(change)
        return write(change)

    monkeypatch.setattr(provider, "write_relationships", observed)
    request = NativeRelationshipMutationRequest(
        actor=organizer_credential.principal,
        credential=organizer_credential.ceiling,
        change=AdministrativeRoleChange(
            RelationshipSubject("principal", transfer.target_uuid),
            "event_administrator",
            TargetRef("event", transfer.event_uuid),
            PolicyEffect.GRANT,
        ),
        scope=transfer.scope,
        idempotency_key=f"event-owner-{transfer.transition_uuid.hex}-grant-target",
        model_id=provider.model_id,
    )
    if defect in {"unbound", "wrong_recipient"}:
        with pytest.raises(ValueError, match="does not match"):
            RetainedEventOwnershipTransferMutationRequest.from_request(request, transfer)
        assert calls == []
        return
    result = apply_native_relationship_mutation(
        RetainedEventOwnershipTransferMutationRequest.from_request(request, transfer), provider
    )
    assert result.state == "denied"
    assert calls == []


def test_new_handoff_rechecks_recipient_at_durable_admission(
    ctf_event, organizer_credential, helper_organizer, monkeypatch
):
    recipient = helper_organizer.identity_principal.uuid
    assign_event_staff(ctf_event.pk, organizer_credential, recipient, "co_organizer")
    provider = configured_authorization_provider()
    check = provider.check

    def deactivate_after_policy(request):
        decision = check(request)
        if request.action == "event.transfer_ownership":
            helper_organizer.is_active = False
            helper_organizer.save(update_fields=["is_active"])
        return decision

    monkeypatch.setattr(provider, "check", deactivate_after_policy)
    with pytest.raises(CTFValidationError) as error:
        transfer_event_ownership(ctf_event.pk, organizer_credential, recipient)
    assert error.value.code == "CTF_TRANSFER_TARGET_INELIGIBLE"
    assert not CTFEventAuthorityTransition.objects.filter(event=ctf_event, kind="transfer").exists()
