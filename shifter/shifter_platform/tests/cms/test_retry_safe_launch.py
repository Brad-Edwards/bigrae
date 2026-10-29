"""Retry-safe range launch orchestration (#2086, ADR-063-R1/R2/R3).

``resolve_retry_recovery`` recovers a bound operation for a replay without minting
or catalog validation; ``bind_first_use_launch`` dispatches + binds on first use. A
replay with the same caller intent recovers the original operation; a different
caller intent conflicts before effects. The heavy RAES create path is mocked.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from django.contrib.auth import get_user_model

from cms.services import RetryKeyConflict, bind_first_use_launch, resolve_retry_recovery
from engine.models import PublicOperationRetryBinding

pytestmark = pytest.mark.django_db
User = get_user_model()

SELECTION = {"agent_id": 5}


class _FakeCtx:
    def __init__(self, request_id):
        self.request_id = request_id


@pytest.fixture
def actor(db):
    user = User.objects.create_user(username="launcher@example.com", email="launcher@example.com")
    from shared.principal_port import principal_for_user
    from workspaces.models import Account
    from workspaces.services import create_account

    create_account(
        kind=Account.Kind.INDIVIDUAL,
        name="Individual account",
        owner=principal_for_user(user),
    )
    return user


def _credential(actor):
    from shared.api.principals import human_session_credential

    return human_session_credential(actor)


def _scope(actor):
    from workspaces.services import individual_account_scope

    return individual_account_scope(_credential(actor).principal)


def _patch_mint(monkeypatch, *, calls):
    def _dispatch(credential, scenario, workspace_uuid=None, scope=None):
        # RAES packages own topology post-PLAT-202; create_range_dispatch no
        # longer takes agents_by_os (see cms.services._retry_safe_launch).
        calls.append(scenario)
        return _FakeCtx(uuid4())

    monkeypatch.setattr("cms.services._retry_safe_launch.create_range_dispatch", _dispatch)
    monkeypatch.setattr("cms.services._retry_safe_launch.operation_id_for_request", lambda request_id: str(uuid4()))


def _bind(actor, scenario, caller_key):
    return bind_first_use_launch(
        _credential(actor),
        scenario=scenario,
        agents_selection=SELECTION,
        workspace_uuid=None,
        caller_key=caller_key,
        scope=_scope(actor),
    )


def test_unbound_key_has_no_recovery(actor, monkeypatch):
    calls: list = []
    _patch_mint(monkeypatch, calls=calls)
    assert (
        resolve_retry_recovery(
            _credential(actor),
            scenario="basic",
            agents_selection=SELECTION,
            workspace_uuid=None,
            caller_key="k1",
            scope=_scope(actor),
        )
        is None
    )
    assert calls == []


def test_first_use_dispatches_and_binds(actor, monkeypatch):
    calls: list = []
    _patch_mint(monkeypatch, calls=calls)
    outcome = _bind(actor, "basic", "k1")
    assert outcome.created is True
    assert calls == ["basic"]
    assert (
        PublicOperationRetryBinding.objects.filter(
            actor_key=str(_credential(actor).principal.uuid), caller_key="k1"
        ).count()
        == 1
    )


def test_replay_same_intent_recovers_without_redispatch(actor, monkeypatch):
    calls: list = []
    _patch_mint(monkeypatch, calls=calls)
    first = _bind(actor, "basic", "k1")

    recovered = resolve_retry_recovery(
        _credential(actor),
        scenario="basic",
        agents_selection=SELECTION,
        workspace_uuid=None,
        caller_key="k1",
        scope=_scope(actor),
    )

    assert recovered is not None
    assert recovered.created is False
    assert recovered.request_id == first.request_id
    assert calls == ["basic"]  # dispatched exactly once


def test_replay_different_intent_conflicts_without_dispatch(actor, monkeypatch):
    calls: list = []
    _patch_mint(monkeypatch, calls=calls)
    _bind(actor, "basic", "k1")

    with pytest.raises(RetryKeyConflict):
        resolve_retry_recovery(
            _credential(actor),
            scenario="advanced",
            agents_selection=SELECTION,
            workspace_uuid=None,
            caller_key="k1",
            scope=_scope(actor),
        )

    assert calls == ["basic"]


def test_distinct_actors_do_not_share_a_key(actor, monkeypatch):
    other = User.objects.create_user(username="other@example.com", email="other@example.com")
    from shared.principal_port import principal_for_user
    from workspaces.models import Account
    from workspaces.services import create_account

    create_account(kind=Account.Kind.INDIVIDUAL, name="Individual account", owner=principal_for_user(other))
    calls: list = []
    _patch_mint(monkeypatch, calls=calls)

    a = _bind(actor, "basic", "k1")
    b = _bind(other, "basic", "k1")

    assert a.created is True
    assert b.created is True
    assert a.request_id != b.request_id


@pytest.mark.parametrize("changed", ["kind", "account", "organization", "workspace"])
def test_explicit_scope_change_conflicts_on_recovery_and_first_use(actor, monkeypatch, changed):
    """An omitted legacy workspace selector cannot erase explicit placement."""
    from shared.identity_scope import ResourceScope

    calls = []
    _patch_mint(monkeypatch, calls=calls)
    original = _scope(actor)
    first = _bind(actor, "basic", "scope-bound")
    account = original.account_uuid
    organization = uuid4()
    alternate = {
        "kind": ResourceScope("installation"),
        "account": ResourceScope("account", uuid4()),
        "organization": ResourceScope("account", account, organization),
        "workspace": ResourceScope("account", account, organization, uuid4()),
    }[changed]
    declaration = {
        "scenario": "basic",
        "agents_selection": SELECTION,
        "workspace_uuid": None,
        "caller_key": "scope-bound",
        "scope": alternate,
    }
    with pytest.raises(RetryKeyConflict):
        resolve_retry_recovery(_credential(actor), **declaration)
    with pytest.raises(RetryKeyConflict):
        bind_first_use_launch(_credential(actor), **declaration)
    assert calls == ["basic"]
    recovered = resolve_retry_recovery(_credential(actor), **(declaration | {"scope": original}))
    assert recovered.request_id == first.request_id
    assert recovered.created is False
    assert PublicOperationRetryBinding.objects.filter(caller_key="scope-bound").count() == 1
