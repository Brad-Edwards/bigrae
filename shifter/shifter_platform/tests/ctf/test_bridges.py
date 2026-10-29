"""Tests for CTF -> CMS bridge scenario selection (launchability filtering)."""

from uuid import uuid4

import pytest
from django.contrib.auth import get_user_model

from cms.models import RaesPackageSource
from ctf import bridges

pytestmark = pytest.mark.django_db

User = get_user_model()


@pytest.fixture
def user(db):
    return User.objects.create_user(username="ctf-bridge@example.com", email="ctf-bridge@example.com")


def _make_raes(user, scenario_id, **overrides):
    fields = {
        "scenario_id": scenario_id,
        "contract_kind": "raes",
        "contract_profile": "shifter",
        "package_ref": "scenario-dev/example/content-packages/example",
        "package_version": "1.0.0",
        "package_digest": "sha256:" + "a" * 64,
        "registered_by": user,
    }
    fields.update(overrides)
    return RaesPackageSource.objects.create(**fields)


class TestCmsListScenariosLaunchability:
    def test_includes_only_launchable_raes_sources(self, user):
        _make_raes(user, "example-ok", conformance_status="passed")
        _make_raes(user, "example-pending", conformance_status="pending")

        ids = {sid for sid, _ in bridges.cms_list_scenarios(user)}

        assert "example-ok" in ids
        assert "example-pending" not in ids


class TestCmsEventRangeControl:
    """CTF forwards the exact event and credential to CMS lifecycle admission."""

    def test_stop_range_forwards_to_pause(self, user, monkeypatch):
        calls: list = []
        event_uuid = uuid4()
        monkeypatch.setattr(
            "cms.services.run_event_range_lifecycle",
            lambda credential, event, rid, op: calls.append((credential, event, rid, op)),
        )

        bridges.cms_stop_range(user, event_uuid, 42)

        assert calls == [(user, event_uuid, 42, "pause")]

    def test_start_range_forwards_to_resume(self, user, monkeypatch):
        calls: list = []
        event_uuid = uuid4()
        monkeypatch.setattr(
            "cms.services.run_event_range_lifecycle",
            lambda credential, event, rid, op: calls.append((credential, event, rid, op)),
        )

        bridges.cms_start_range(user, event_uuid, 7)

        assert calls == [(user, event_uuid, 7, "resume")]
