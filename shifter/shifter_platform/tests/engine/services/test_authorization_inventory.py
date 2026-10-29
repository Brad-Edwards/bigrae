"""Delegation inventory uses the public CMS range policy identity."""

from uuid import uuid4

import pytest

from engine.models import Range
from engine.services._authorization_inventory import list_authorization_range_uuids

pytestmark = pytest.mark.django_db


def test_workspace_range_inventory_returns_public_policy_uuid():
    public_uuid, native_uuid = uuid4(), uuid4()
    Range.objects.create(
        uuid=native_uuid,
        cms_range_uuid=public_uuid,
        scope_kind="account",
        account_id=101,
        organization_id=102,
        workspace_id=103,
    )
    result = list_authorization_range_uuids((103,), 10)
    assert result == (public_uuid,)
    assert native_uuid not in result
