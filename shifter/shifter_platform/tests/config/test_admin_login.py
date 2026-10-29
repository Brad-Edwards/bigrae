"""Django admin sign-in goes through the provider login, never a password form (#2617)."""

from __future__ import annotations

import pytest
from django.test import Client
from django.urls import reverse

pytestmark = pytest.mark.django_db

ADMIN_LOGIN = "/admin/login/"


@pytest.fixture
def staff_user(django_user_model):
    user = django_user_model.objects.create_user(
        username="staff@example.com", email="staff@example.com", password="Synthetic-pass-12345"
    )
    user.is_staff = True
    user.save(update_fields=["is_staff"])
    return user


def test_admin_login_sends_staff_to_the_provider_login(staff_user):
    client = Client()

    entry = client.get("/admin/")
    form = client.get(ADMIN_LOGIN)

    assert entry.status_code == 302
    assert entry.url.startswith(ADMIN_LOGIN)
    assert form.status_code == 302
    assert form.url == reverse("platform_login")


def test_admin_login_never_checks_a_local_password(staff_user):
    client = Client()

    response = client.post(ADMIN_LOGIN, {"username": staff_user.username, "password": "Synthetic-pass-12345"})

    assert response.status_code == 405
    assert "_auth_user_id" not in client.session


def test_signed_in_staff_still_reach_django_admin(staff_user):
    client = Client()
    client.force_login(staff_user, backend="config.auth.PlatformModelBackend")

    assert client.get("/admin/").status_code == 200
