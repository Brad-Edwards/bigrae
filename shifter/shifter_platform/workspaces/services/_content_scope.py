"""Read-only organization scope for private content, separate from admin powers."""

from collections.abc import Iterable
from uuid import UUID

from django.contrib.auth.models import AnonymousUser, User

from workspaces.models import Organization, OrganizationMembership, Workspace, WorkspaceMembership


def content_organization_uuids(actor: User | AnonymousUser | None) -> frozenset[UUID]:
    """Return current organization memberships, including active workspace seats.

    Staff status does not grant access. The explicit platform superuser override
    may inspect all organizations; inactive and anonymous callers see none.
    """
    if not isinstance(actor, User) or not actor.is_authenticated or not actor.is_active:
        return frozenset()
    if actor.is_superuser:
        return frozenset(Organization.objects.values_list("uuid", flat=True))
    organizations = OrganizationMembership.objects.filter(user=actor).values_list("organization__uuid", flat=True)
    workspaces = WorkspaceMembership.objects.filter(user=actor, workspace__archived_at__isnull=True).values_list(
        "workspace__organization__uuid", flat=True
    )
    return frozenset((*organizations, *workspaces))


def organization_scope_ids(organization_uuid: UUID, workspace_ids: Iterable[int]) -> tuple[int | None, frozenset[int]]:
    """Return the organization's internal id and the given workspaces it contains.

    Read-only ancestry for a caller that already authorized ``organization_uuid``.
    Unknown organizations return ``(None, frozenset())``.
    """
    organization_id = Organization.objects.filter(uuid=organization_uuid).values_list("pk", flat=True).first()
    if organization_id is None:
        return None, frozenset()
    contained = Workspace.objects.filter(organization_id=organization_id, pk__in=set(workspace_ids))
    return organization_id, frozenset(contained.values_list("pk", flat=True))
