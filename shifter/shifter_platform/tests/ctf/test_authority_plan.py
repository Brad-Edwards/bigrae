"""Owning command recovery retains exact effects and bounded HTTP outcomes."""

import pytest

from ctf.exceptions import CTFStateError
from ctf.models import CTFEventAuthorityTransition
from ctf.services.event import assign_event_staff, transfer_event_ownership
from shared.authorization import AuthorizationProviderError, configured_authorization_provider
from tests.ctf._api_flow_helpers import call_json
from tests.ctf.test_event_staff_api import helper_organizer as helper_organizer

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize("phase", ["preflight", "publication"])
def test_complete_handoff_admission_outage_leaves_no_pending_command(
    ctf_event, organizer_credential, helper_organizer, monkeypatch, phase
):
    recipient = helper_organizer.identity_principal.uuid
    assign_event_staff(ctf_event.pk, organizer_credential, recipient, "co_organizer")
    provider = configured_authorization_provider()
    batch = provider.batch_check

    def unavailable(requests):
        if (
            phase == "preflight"
            or CTFEventAuthorityTransition.objects.filter(event=ctf_event, state="pending").exists()
        ):
            raise AuthorizationProviderError("Synthetic evaluator outage")
        return batch(requests)

    monkeypatch.setattr(provider, "batch_check", unavailable)
    with pytest.raises(CTFStateError):
        transfer_event_ownership(ctf_event.pk, organizer_credential, recipient)
    assert CTFEventAuthorityTransition.objects.filter(event=ctf_event, state="pending").exists() == (
        phase == "publication"
    )
    from workspaces.models import AuthorizationOperation

    assert not AuthorizationOperation.objects.filter(state="denied").exists()
    monkeypatch.setattr(provider, "batch_check", batch)
    assert transfer_event_ownership(ctf_event.pk, organizer_credential, recipient).owner_principal_uuid == recipient


@pytest.mark.parametrize("fresh_administrator", [False, True])
def test_http_transfer_exposes_pending_receipt_and_exact_retry(
    ctf_event,
    organizer_credential,
    helper_organizer,
    authenticated_organizer_client,
    admin_user,
    monkeypatch,
    fresh_administrator,
):
    recipient = helper_organizer.identity_principal.uuid
    assign_event_staff(ctf_event.pk, organizer_credential, recipient, "co_organizer")
    provider = configured_authorization_provider()
    read = provider.read_relationships

    def unavailable(change):
        raise AuthorizationProviderError("Synthetic acknowledgement loss")

    monkeypatch.setattr(provider, "read_relationships", unavailable)
    response = call_json(
        authenticated_organizer_client,
        "post",
        "api_event_transfer_ownership",
        kwargs={"event_id": ctf_event.pk},
        body={"principal_uuid": str(recipient)},
    )
    assert response.status_code == 503
    from django.test import Client

    administrator = Client()
    administrator.force_login(admin_user)
    recovering = administrator if fresh_administrator else authenticated_organizer_client
    listing = call_json(recovering, "get", "api_event_staff", kwargs={"event_id": ctf_event.pk})
    assert listing.status_code == 200
    receipt = listing.json()["pending_transition"]
    assert receipt["kind"] == "transfer" and receipt["target_principal_uuid"] == str(recipient)
    assert receipt["completed_effects"] < receipt["total_effects"]
    from django.test import Client

    delegated = Client()
    delegated.force_login(helper_organizer)
    denied = call_json(delegated, "get", "api_event_staff", kwargs={"event_id": ctf_event.pk})
    assert denied.status_code == 403 and "pending_transition" not in denied.json()
    monkeypatch.setattr(provider, "read_relationships", read)
    response = call_json(
        recovering,
        "post",
        "api_event_transfer_ownership",
        kwargs={"event_id": ctf_event.pk},
        body={"principal_uuid": str(recipient)},
    )
    assert response.status_code == 200
    assert not CTFEventAuthorityTransition.objects.filter(event=ctf_event, state="pending").exists()


def test_interrupted_handoff_uses_retained_effects_after_role_catalog_changes(
    ctf_event, organizer_credential, helper_organizer, monkeypatch
):
    from ctf.services.event import staff

    recipient = helper_organizer.identity_principal.uuid
    assign_event_staff(ctf_event.pk, organizer_credential, recipient, "co_organizer")
    provider = configured_authorization_provider()
    write = provider.write_relationships

    def unavailable(change):
        raise AuthorizationProviderError("Synthetic write outage")

    monkeypatch.setattr(provider, "write_relationships", unavailable)
    with pytest.raises(CTFStateError):
        transfer_event_ownership(ctf_event.pk, organizer_credential, recipient)
    transition = CTFEventAuthorityTransition.objects.get(event=ctf_event, state="pending")
    original_plan = transition.effect_plan
    monkeypatch.setattr(staff, "actions_for_role", lambda role: frozenset({"event.read"}))
    monkeypatch.setattr(provider, "write_relationships", write)
    assert transfer_event_ownership(ctf_event.pk, organizer_credential, recipient).owner_principal_uuid == recipient
    transition.refresh_from_db()
    assert transition.effect_plan == original_plan
    from workspaces.models import AuthorizationOperation

    keys = {effect["key"] for effect in original_plan["effects"]}
    assert (
        set(
            AuthorizationOperation.objects.filter(idempotency_key__in=keys, state="confirmed").values_list(
                "idempotency_key", flat=True
            )
        )
        == keys
    )


def test_full_plan_explicit_denial_precedes_pending_intent(
    ctf_event, organizer_credential, helper_organizer, monkeypatch
):
    from ctf.exceptions import CTFPermissionError
    from shared.authorization import AuthorizationDecision, DecisionKind

    recipient = helper_organizer.identity_principal.uuid
    assign_event_staff(ctf_event.pk, organizer_credential, recipient, "co_organizer")
    provider = configured_authorization_provider()
    batch = provider.batch_check

    def denied(requests):
        if any(request.action == "event.manage_config" for request in requests):
            return tuple(AuthorizationDecision(DecisionKind.DENIED, "policy_denied") for _ in requests)
        return batch(requests)

    monkeypatch.setattr(provider, "batch_check", denied)
    with pytest.raises(CTFPermissionError):
        transfer_event_ownership(ctf_event.pk, organizer_credential, recipient)
    assert not CTFEventAuthorityTransition.objects.filter(event=ctf_event, state="pending").exists()


@pytest.mark.parametrize(
    "field,value", [("effect_plan", {"version": 999}), ("effect_plan_digest", "altered"), ("kind", "revoke")]
)
def test_database_refuses_altered_admitted_effect_plan(ctf_event, organizer_credential, helper_organizer, field, value):
    from django.db import DatabaseError, transaction

    recipient = helper_organizer.identity_principal.uuid
    assign_event_staff(ctf_event.pk, organizer_credential, recipient, "co_organizer")
    transition = CTFEventAuthorityTransition.objects.get(event=ctf_event)
    with pytest.raises(DatabaseError), transaction.atomic():
        CTFEventAuthorityTransition.objects.filter(pk=transition.pk).update(**{field: value})
    transition.refresh_from_db()
    assert transition.effect_plan["version"] == 1 and transition.kind == "assign"


def test_unversioned_in_process_plan_is_refused(ctf_event, organizer_credential, helper_organizer):
    from ctf.services.event.authority_plan import retained_plan

    recipient = helper_organizer.identity_principal.uuid
    assign_event_staff(ctf_event.pk, organizer_credential, recipient, "co_organizer")
    transition = CTFEventAuthorityTransition.objects.get(event=ctf_event)
    transition.effect_plan = {"version": 999}
    with pytest.raises(CTFStateError):
        retained_plan(transition)


def test_native_effect_cannot_disappear_from_batched_preflight(ctf_event, organizer_credential, helper_organizer):
    from dataclasses import replace

    from ctf.exceptions import CTFPermissionError
    from shared.authorization import CredentialCeiling

    recipient = helper_organizer.identity_principal.uuid
    assign_event_staff(ctf_event.pk, organizer_credential, recipient, "co_organizer")
    narrowed = replace(
        organizer_credential,
        ceiling=CredentialCeiling(organizer_credential.ceiling.actions - {"event.delegate_authorization"}),
    )
    with pytest.raises(CTFPermissionError):
        transfer_event_ownership(ctf_event.pk, narrowed, recipient)
    assert not CTFEventAuthorityTransition.objects.filter(event=ctf_event, state="pending").exists()


@pytest.mark.parametrize("unavailable,expected", [(True, 503), (False, 403)])
def test_transfer_http_distinguishes_initial_policy_outage_from_denial(
    ctf_event,
    organizer_credential,
    helper_organizer,
    authenticated_organizer_client,
    monkeypatch,
    unavailable,
    expected,
):
    from shared.authorization import AuthorizationDecision, DecisionKind

    recipient = helper_organizer.identity_principal.uuid
    assign_event_staff(ctf_event.pk, organizer_credential, recipient, "co_organizer")
    provider = configured_authorization_provider()
    check = provider.check

    def failed(request):
        if request.action == "event.transfer_ownership":
            return AuthorizationDecision(
                DecisionKind.EVALUATOR_ERROR if unavailable else DecisionKind.DENIED,
                "evaluator_unavailable" if unavailable else "policy_denied",
            )
        return check(request)

    monkeypatch.setattr(provider, "check", failed)
    response = call_json(
        authenticated_organizer_client,
        "post",
        "api_event_transfer_ownership",
        kwargs={"event_id": ctf_event.pk},
        body={"principal_uuid": str(recipient)},
    )
    assert response.status_code == expected
    assert not CTFEventAuthorityTransition.objects.filter(event=ctf_event, state="pending").exists()


@pytest.mark.parametrize("previous_role", [None, "moderator"])
def test_fully_admitted_staff_plan_settles_after_subject_deactivation(
    ctf_event, organizer_credential, helper_organizer, admin_user, monkeypatch, previous_role
):
    from workspaces.models import AuthorizationOperation

    subject = helper_organizer.identity_principal.uuid
    if previous_role:
        assign_event_staff(ctf_event.pk, organizer_credential, subject, previous_role)
    provider = configured_authorization_provider()
    write = provider.write_relationships

    def unavailable(change):
        raise AuthorizationProviderError("Synthetic first-effect write outage")

    monkeypatch.setattr(provider, "write_relationships", unavailable)
    with pytest.raises(CTFStateError):
        assign_event_staff(ctf_event.pk, organizer_credential, subject, "co_organizer")
    pending = CTFEventAuthorityTransition.objects.get(event=ctf_event, state="pending")
    plan = pending.effect_plan
    keys = {effect["key"] for effect in plan["effects"]}
    assert AuthorizationOperation.objects.filter(idempotency_key__in=keys).count() == 1
    helper_organizer.is_active = False
    helper_organizer.save(update_fields=["is_active"])
    monkeypatch.setattr(provider, "write_relationships", write)
    from django.test import Client

    administrator = Client()
    administrator.force_login(admin_user)
    listing = call_json(administrator, "get", "api_event_staff", kwargs={"event_id": ctf_event.pk})
    assert listing.status_code == 200
    receipt = listing.json()["pending_transition"]
    assert receipt["id"] == str(pending.pk) and receipt["kind"] == "assign"
    response = call_json(
        administrator,
        "post",
        "api_event_staff",
        kwargs={"event_id": ctf_event.pk},
        body={"principal_uuid": receipt["target_principal_uuid"], "role": receipt["requested_role"]},
    )
    assert response.status_code == 201
    assert response.json()["principal_uuid"] == str(subject) and response.json()["role"] == "co_organizer"
    pending.refresh_from_db()
    assert pending.state == "confirmed" and pending.effect_plan == plan
    assert (
        set(
            AuthorizationOperation.objects.filter(idempotency_key__in=keys, state="confirmed").values_list(
                "idempotency_key", flat=True
            )
        )
        == keys
    )


def test_new_staff_plan_cannot_grant_an_inactive_subject(ctf_event, organizer_credential, helper_organizer):
    from ctf.exceptions import CTFNotFoundError

    helper_organizer.is_active = False
    helper_organizer.save(update_fields=["is_active"])
    with pytest.raises(CTFNotFoundError):
        assign_event_staff(ctf_event.pk, organizer_credential, helper_organizer.identity_principal.uuid, "co_organizer")
    assert not CTFEventAuthorityTransition.objects.filter(event=ctf_event, state="pending").exists()


def test_new_staff_self_grant_does_not_leave_pending_intent(ctf_event, admin_user, live_session_credential):
    from ctf.exceptions import CTFPermissionError

    administrator = live_session_credential(admin_user)
    with pytest.raises(CTFPermissionError):
        assign_event_staff(ctf_event.pk, administrator, administrator.principal.uuid, "co_organizer")
    assert not CTFEventAuthorityTransition.objects.filter(event=ctf_event, state="pending").exists()


@pytest.mark.parametrize("revoked", [False, True])
def test_retained_staff_settlement_requires_current_proof_and_remaining_delegation(
    ctf_event, organizer_credential, helper_organizer, admin_user, live_session_credential, monkeypatch, revoked
):
    from ctf.exceptions import CTFError
    from management.models import PrincipalSession
    from shared.authorization import AuthorizationDecision, DecisionKind
    from workspaces.models import AuthorizationOperation

    provider = configured_authorization_provider()
    write = provider.write_relationships
    subject = helper_organizer.identity_principal.uuid

    def unavailable(change):
        raise AuthorizationProviderError("Synthetic admission interruption")

    monkeypatch.setattr(provider, "write_relationships", unavailable)
    with pytest.raises(CTFStateError):
        assign_event_staff(ctf_event.pk, organizer_credential, subject, "co_organizer")
    helper_organizer.is_active = False
    helper_organizer.save(update_fields=["is_active"])
    administrator = live_session_credential(admin_user)
    writes = []

    def observed(change):
        writes.append(change)
        return write(change)

    monkeypatch.setattr(provider, "write_relationships", observed)
    if revoked:
        PrincipalSession.objects.filter(credential_uuid=administrator.credential_uuid).delete()
    else:
        monkeypatch.setattr(
            provider,
            "batch_check",
            lambda requests: tuple(AuthorizationDecision(DecisionKind.DENIED, "policy_denied") for _ in requests),
        )
    with pytest.raises(CTFError):
        assign_event_staff(ctf_event.pk, administrator, subject, "co_organizer")
    assert writes == [] and AuthorizationOperation.objects.count() == 1
    assert CTFEventAuthorityTransition.objects.filter(event=ctf_event, state="pending").exists()


@pytest.mark.parametrize("defect", ["subject", "effect", "provider", "unbound"])
def test_retained_staff_effect_rejects_mismatched_or_unbound_admission(
    ctf_event, organizer_credential, helper_organizer, admin_user, live_session_credential, monkeypatch, defect
):
    from dataclasses import replace
    from uuid import uuid4

    from shared.authorization import PolicyEffect, RelationshipSubject, TargetRef
    from shared.authorization.inventory import resolve_event_authority_plan
    from workspaces.models import AuthorizationOperation
    from workspaces.services import (
        AuthorizationMutationConflict,
        PolicyMutationRequest,
        RetainedEventAuthorityPolicyMutationRequest,
        apply_policy_mutation,
    )

    provider = configured_authorization_provider()
    subject = helper_organizer.identity_principal.uuid

    def unavailable(change):
        raise AuthorizationProviderError("Synthetic interruption")

    monkeypatch.setattr(provider, "write_relationships", unavailable)
    with pytest.raises(CTFStateError):
        assign_event_staff(ctf_event.pk, organizer_credential, subject, "co_organizer")
    pending = CTFEventAuthorityTransition.objects.get(event=ctf_event, state="pending")
    plan = resolve_event_authority_plan(pending.pk)
    admitted_keys = set(AuthorizationOperation.objects.values_list("idempotency_key", flat=True))
    step = next(effect for effect in plan.effects if effect.key not in admitted_keys)
    actor = live_session_credential(admin_user)
    request = PolicyMutationRequest(
        actor.principal,
        actor.ceiling,
        RelationshipSubject("principal", uuid4() if defect == "subject" else subject),
        step.action,
        TargetRef("event", ctf_event.pk),
        plan.scope,
        PolicyEffect.REVOKE if defect == "effect" else PolicyEffect(step.effect),
        step.key,
        provider.model_id,
    )
    if defect == "provider":
        plan = replace(plan, store_id="another-store")
    elif defect == "unbound":
        plan = replace(plan, transition_uuid=uuid4())
    writes = []
    monkeypatch.setattr(provider, "write_relationships", lambda change: writes.append(change))
    if defect in {"subject", "effect"}:
        with pytest.raises(ValueError, match="does not match"):
            RetainedEventAuthorityPolicyMutationRequest.from_request(request, plan)
    else:
        admitted = RetainedEventAuthorityPolicyMutationRequest.from_request(request, plan)
        if defect == "provider":
            with pytest.raises(AuthorizationMutationConflict, match="provider binding"):
                apply_policy_mutation(admitted, provider)
        else:
            assert apply_policy_mutation(admitted, provider).state == "denied"
    assert writes == []
