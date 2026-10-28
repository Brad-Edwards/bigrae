"""The owning organizer reaches the CTF organizer pages and APIs for its event."""

from __future__ import annotations

import pytest
from django.urls import reverse


@pytest.mark.django_db
class TestOwnerCanAccess:
    """Verify the owning organizer CAN access these views (not just that others can't).

    Integration assertions (ADR-019): the owning ``organizer_user`` reaches each
    surface for its own ``ctf_event`` through the test client and real template /
    JSON rendering, instead of patching first-party ``render``/topology.
    """

    def test_range_list_allows_owner(self, client, organizer_user, ctf_event):
        client.force_login(organizer_user)
        response = client.get(reverse("ctf:admin_range_list", kwargs={"event_id": ctf_event.id}))
        assert response.status_code == 200

    def test_notification_list_allows_owner(self, client, organizer_user, ctf_event):
        client.force_login(organizer_user)
        response = client.get(reverse("ctf:admin_notification_list", kwargs={"event_id": ctf_event.id}))
        assert response.status_code == 200

    def test_notification_create_allows_owner(self, client, organizer_user, ctf_event):
        client.force_login(organizer_user)
        response = client.get(reverse("ctf:admin_notification_create", kwargs={"event_id": ctf_event.id}))
        assert response.status_code == 200

    def test_api_range_list_allows_owner(self, client, organizer_user, ctf_event):
        client.force_login(organizer_user)
        response = client.get(reverse("v1:ctf:api_range_list", kwargs={"event_id": ctf_event.id}))
        assert response.status_code == 200

    def test_api_notification_list_allows_owner(self, client, organizer_user, ctf_event):
        client.force_login(organizer_user)
        response = client.get(reverse("v1:ctf:api_notification_list", kwargs={"event_id": ctf_event.id}))
        assert response.status_code == 200
