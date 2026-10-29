"""Native Django admin exposes policy-authorized reads and no identity writes (#2617)."""

from __future__ import annotations

import pytest
from django.contrib.auth.models import Group, Permission
from django.test import Client

from management.services import get_user_profile
from tests.workspaces.test_account_policy_admin import Provider

pytestmark = pytest.mark.django_db


@pytest.fixture(params=[False, True], ids=["staff", "superuser"])
def administrator(request, django_user_model):
    """Legacy flags and model permissions cannot reopen the native write boundary."""
    user = django_user_model.objects.create_user(
        username="user-admin@example.com",
        email="user-admin@example.com",
        password="Synthetic-pass-12345",
        is_staff=True,
        is_superuser=request.param,
    )
    codenames = ["view_user", "change_user", "view_group", "change_group", "view_userprofile", "change_userprofile"]
    user.user_permissions.add(*Permission.objects.filter(codename__in=codenames))
    return user


def test_admin_cannot_write_identity_or_privilege_records(administrator, monkeypatch):
    monkeypatch.setattr("shared.authorization.port._provider_factory", Provider)
    group = Group.objects.create(name="Synthetic Operators")
    profile = get_user_profile(administrator)
    original_superuser = administrator.is_superuser
    client = Client()
    client.force_login(administrator, backend="config.auth.PlatformModelBackend")

    own_change = f"/admin/auth/user/{administrator.pk}/change/"
    assert client.get(own_change).status_code == 200
    escalation = client.post(
        own_change,
        {"username": administrator.username, "is_active": "on", "is_staff": "on", "is_superuser": "on"},
    )
    assert escalation.status_code == 403
    administrator.refresh_from_db()
    assert administrator.is_superuser is original_superuser
    original_password = administrator.password
    password_path = f"/admin/auth/user/{administrator.pk}/password/"
    # ModelAdmin's generic object redirect exists; no password-change view is mounted.
    assert client.get(password_path)["Location"] == f"{password_path}change/"
    password_attempt = client.post(password_path, {"password1": "Changed-pass-12345"})
    assert password_attempt.status_code == 302
    assert password_attempt["Location"] == f"{password_path}change/"
    assert client.post(password_attempt["Location"], {"password1": "Changed-pass-12345"}).status_code == 403
    administrator.refresh_from_db()
    assert administrator.password == original_password
    assert client.post(f"/admin/auth/group/{group.pk}/change/", {"name": "changed"}).status_code == 404
    assert client.post(f"/admin/management/userprofile/{profile.pk}/change/", {}).status_code == 403
    assert client.get("/admin/auth/user/add/").status_code == 403
    group.refresh_from_db()
    assert group.name == "Synthetic Operators"


def test_legacy_admin_flags_do_not_bypass_read_policy(administrator, monkeypatch):
    monkeypatch.setattr("shared.authorization.port._provider_factory", lambda: Provider(allowed=False))
    client = Client()
    client.force_login(administrator, backend="config.auth.PlatformModelBackend")

    assert client.get(f"/admin/auth/user/{administrator.pk}/change/").status_code == 403
