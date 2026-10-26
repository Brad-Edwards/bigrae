"""CTF-owned identity cutover keeps revoked staff authority revoked."""

import pytest

from ctf.models import CTFEventStaff
from shared.authorization.model import grant_relation_for_action

pytestmark = pytest.mark.django_db


def test_revoked_organizer_staff_blocks_cutover_and_projects_no_grants(ctf_event, co_organizer_user) -> None:
    from ctf.services.identity_cutover import identity_cutover_disposition, identity_cutover_relationships

    principal_uuid = co_organizer_user.identity_principal.uuid
    CTFEventStaff.objects.create(
        event=ctf_event,
        user=co_organizer_user,
        principal_uuid=principal_uuid,
        role="co_organizer",
    )
    co_organizer_user.groups.clear()

    _, blockers = identity_cutover_disposition()
    relationships = identity_cutover_relationships()

    assert {item["reason"] for item in blockers} >= {"ctf_staff_organizer_eligibility_revoked"}
    assert (
        f"principal:{principal_uuid}",
        grant_relation_for_action("event.delete"),
        f"event:{ctf_event.pk}",
    ) not in relationships
