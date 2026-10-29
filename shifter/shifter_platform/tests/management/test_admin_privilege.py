"""Django admin cannot raise a non-superuser staff account to superuser (#2617).

The Administer API keeps the ``auth.change_user`` tier below superuser. The
stock Django admin would let that tier edit ``is_superuser``, group
permissions, profile identity bindings, and passwords, so the admin writes for
users, groups, and profiles require an active superuser.
"""

from __future__ import annotations

import pytest
from django.contrib.auth.models import Group, Permission
from django.test import Client

from management.services import get_user_profile

pytestmark = pytest.mark.django_db


@pytest.fixture
def user_administrator(django_user_model):
    """A staff account in the Administer user-admin tier, not a superuser."""
    user = django_user_model.objects.create_user(
        username="user-admin@example.com", email="user-admin@example.com", password="Synthetic-pass-12345"
    )
    user.is_staff = True
    user.save(update_fields=["is_staff"])
    codenames = ["view_user", "change_user", "view_group", "change_group", "view_userprofile", "change_userprofile"]
    user.user_permissions.add(*Permission.objects.filter(codename__in=codenames))
    return user


@pytest.fixture
def superuser(django_user_model):
    return django_user_model.objects.create_superuser(
        username="root@example.com", email="root@example.com", password="Synthetic-pass-12345"
    )


def test_user_administrator_cannot_write_identity_or_privilege_records(user_administrator, superuser):
    group = Group.objects.create(name="Synthetic Operators")
    profile = get_user_profile(user_administrator)
    client = Client()
    client.force_login(user_administrator, backend="config.auth.PlatformModelBackend")

    own_change = f"/admin/auth/user/{user_administrator.pk}/change/"
    assert client.get(own_change).status_code == 200  # read access stays
    escalation = client.post(
        own_change,
        {
            "username": user_administrator.username,
            "email": user_administrator.email,
            "is_active": "on",
            "is_staff": "on",
            "is_superuser": "on",
            "date_joined_0": "2026-01-01",
            "date_joined_1": "00:00:00",
        },
    )
    assert escalation.status_code == 403
    user_administrator.refresh_from_db()
    assert user_administrator.is_superuser is False

    assert client.get(f"/admin/auth/user/{superuser.pk}/password/").status_code == 403
    assert client.post(f"/admin/auth/group/{group.pk}/change/", {"name": "x"}).status_code == 403
    assert client.post(f"/admin/management/userprofile/{profile.pk}/change/", {}).status_code == 403
    assert client.get("/admin/auth/user/add/").status_code == 403


def test_superuser_keeps_django_admin_user_writes(user_administrator, superuser):
    client = Client()
    client.force_login(superuser, backend="config.auth.PlatformModelBackend")

    assert client.get(f"/admin/auth/user/{user_administrator.pk}/password/").status_code == 200
    renamed = client.post(f"/admin/auth/group/{Group.objects.create(name='g').pk}/change/", {"name": "renamed"})
    assert renamed.status_code == 302
    assert Group.objects.filter(name="renamed").exists()
