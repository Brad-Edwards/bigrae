"""Tests for the dev/test-only ``seed_e2e`` management command."""

from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import override_settings

from shared.authorization import AdministrativeRoleChange, PolicyEffect, RelationshipSubject, TargetRef
from tests.workspaces.test_account_policy_admin import Provider
from workspaces.management.commands.seed_e2e import (
    STAFF_EMAIL,
    STANDARD_EMAIL,
    THREAT_EMAIL,
)
from workspaces.models import Account, HierarchyPublication, OrganizationMembership, WorkspaceMembership

User = get_user_model()

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def seed_provider(monkeypatch):
    provider = Provider()
    monkeypatch.setattr("shared.authorization.port._provider_factory", lambda: provider)
    return provider


def test_seed_creates_synthetic_actors_and_org_authority(seed_provider):
    call_command("seed_e2e")

    staff = User.objects.get(username=STAFF_EMAIL)
    assert staff.is_staff is True
    assert staff.is_active is True
    business = Account.objects.get(kind="team", name="E2E Team")
    grant = AdministrativeRoleChange(
        RelationshipSubject("principal", staff.identity_principal.uuid),
        "account_administrator",
        TargetRef("account", business.uuid),
        PolicyEffect.GRANT,
    )
    assert grant.grant_tuple in seed_provider.relationships
    assert not OrganizationMembership.objects.filter(user=staff).exists()
    assert not WorkspaceMembership.objects.filter(user=staff).exists()
    assert not HierarchyPublication.objects.exclude(state="confirmed").exists()

    threat = User.objects.get(username=THREAT_EMAIL)
    assert threat.groups.filter(name="Threat Research").exists()
    assert threat.is_staff is False

    standard = User.objects.get(username=STANDARD_EMAIL)
    assert standard.is_active is True
    assert standard.is_staff is False


def test_seed_is_idempotent():
    call_command("seed_e2e")
    call_command("seed_e2e")

    assert User.objects.filter(username=STAFF_EMAIL).count() == 1
    staff = User.objects.get(username=STAFF_EMAIL)
    assert Account.objects.filter(kind="team", name="E2E Team").count() == 1
    assert Account.objects.filter(kind="individual").count() == 4
    assert not OrganizationMembership.objects.filter(user=staff).exists()
    assert Group.objects.filter(name="Threat Research").count() == 1


@override_settings(DEBUG=False, ENVIRONMENT="production")
def test_seed_refuses_production_posture():
    with pytest.raises(CommandError):
        call_command("seed_e2e")

    # Fail-closed: nothing is created when the gate refuses.
    assert not User.objects.filter(username=STAFF_EMAIL).exists()
    assert not User.objects.filter(username=THREAT_EMAIL).exists()
