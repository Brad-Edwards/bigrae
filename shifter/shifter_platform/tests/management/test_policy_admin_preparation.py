"""Dormant Django-admin registration cannot become an unguarded write path."""

import pytest
from django.contrib.admin import AdminSite
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied
from django.test import RequestFactory

from management.models import UserProfile
from tests.workspaces.test_account_policy_admin import Provider, _actor

pytestmark = pytest.mark.django_db


def test_prepared_profile_admin_is_policy_guarded_and_every_write_is_disabled(monkeypatch):
    from management.policy_admin import PolicyUserProfileAdmin

    actor = _actor("human", actions=frozenset({"installation.manage_principals"}))
    user = get_user_model().objects.get(identity_principal__uuid=actor.principal.uuid)
    request = RequestFactory().get("/admin/")
    request.user = user
    instance = PolicyUserProfileAdmin(UserProfile, AdminSite())
    monkeypatch.setattr("shared.admin_policy.configured_authorization_provider", lambda: Provider(allowed=False))
    assert not instance.has_view_permission(request)
    monkeypatch.setattr("shared.admin_policy.configured_authorization_provider", Provider)
    assert instance.has_view_permission(request)
    assert not instance.has_add_permission(request)
    assert not instance.has_change_permission(request, user.profile)
    assert not instance.has_delete_permission(request, user.profile)
    with pytest.raises(PermissionDenied):
        instance.save_model(request, user.profile, None, True)
    with pytest.raises(PermissionDenied):
        instance.delete_model(request, user.profile)


def test_live_profile_admin_remains_unchanged_before_cutover():
    from django.contrib import admin

    from management.admin import UserProfileAdmin

    assert isinstance(admin.site._registry[UserProfile], UserProfileAdmin)


def test_prepared_admin_site_admits_policy_session_without_staff_flags():
    from shared.admin_policy import PolicyAdminSite

    actor = _actor("human", actions=frozenset({"installation.manage_principals"}))
    user = get_user_model().objects.get(identity_principal__uuid=actor.principal.uuid)
    request = RequestFactory().get("/admin/")
    request.user = user
    site = PolicyAdminSite()
    assert not user.is_staff and not user.is_superuser
    assert site.has_permission(request)
    user.is_active = False
    user.save(update_fields=["is_active"])
    assert not site.has_permission(request)


def test_prepared_user_metadata_retains_validation_and_no_authority_writes(monkeypatch):
    from management import admin_services
    from management.models import ProviderBinding

    actor = _actor("service", actions=frozenset({"installation.manage_principals"}))
    user = get_user_model().objects.create_user(username="metadata-subject", email="before@example.test")
    changed = admin_services.update_policy_user_metadata(
        actor, Provider(), user.pk, {"first_name": "Synthetic", "email": "after@example.test"}
    )
    assert changed.email == "after@example.test" and changed.first_name == "Synthetic"
    assert not changed.is_staff and not changed.is_superuser
    assert not ProviderBinding.objects.filter(principal__user=changed).exists()
    with pytest.raises(ValueError):
        admin_services.update_policy_user_metadata(actor, Provider(), user.pk, {"is_superuser": True})
    with pytest.raises(PermissionError):
        admin_services.update_policy_user_metadata(
            actor, Provider(allowed=False), user.pk, {"email": "denied@example.test"}
        )
    user.refresh_from_db()
    assert user.email == "after@example.test"


def test_user_metadata_uniqueness_race_is_a_bounded_validation_error(monkeypatch):
    from management import admin_services

    actor = _actor("service", actions=frozenset({"installation.manage_principals"}))
    user = get_user_model().objects.create_user(username="rename-source")
    get_user_model().objects.create_user(username="occupied-username")
    # The database remains authoritative if a competing claim wins after validation.
    monkeypatch.setattr(get_user_model(), "full_clean", lambda *_args, **_kwargs: None)
    with pytest.raises(ValueError, match="metadata"):
        admin_services.update_policy_user_metadata(actor, Provider(), user.pk, {"username": "occupied-username"})
    user.refresh_from_db()
    assert user.username == "rename-source"
