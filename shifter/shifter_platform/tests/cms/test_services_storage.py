"""Native installer byte accounting is scoped before aggregation."""

import pytest

from cms.exceptions import CMSError
from tests.cms._native_asset_fixtures import (
    content_actor,
    content_administrator,
    installer_upload,
    scoped_agents,
    storage_client,
)

__all__ = ["content_actor", "content_administrator", "installer_upload", "scoped_agents", "storage_client"]
pytestmark = pytest.mark.django_db


def test_native_storage_counts_only_live_installers_in_the_admitted_scope(content_actor, scoped_agents):
    from django.utils import timezone

    from cms.services import get_storage_used

    actor = content_actor
    assert get_storage_used(actor.credential, scope=actor.scope) == 100
    with pytest.raises(CMSError):
        get_storage_used(actor.credential, scope=actor.sibling)
    scoped_agents[0].deleted_at = timezone.now()
    scoped_agents[0].save(update_fields=["deleted_at"])
    assert get_storage_used(actor.credential, scope=actor.scope) == 0


def test_unproven_storage_lookup_is_a_denial_even_for_staff(django_user_model):
    from cms.services import get_storage_used

    user = django_user_model.objects.create_superuser(username="storage-staff", password="unused")
    with pytest.raises(CMSError, match="Content unavailable"):
        get_storage_used(user)
