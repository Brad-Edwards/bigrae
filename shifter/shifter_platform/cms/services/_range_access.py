"""Workspace-authorized facades for interactive range instance access."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from uuid import UUID

from django.core.exceptions import ValidationError

from cms.exceptions import CMSError
from cms.models import Instance, RangeInstance, Request
from shared.credentials import CredentialContext, resolve_current_credential
from shared.operation_authority import credential_reference_fields
from shared.operation_envelope import canonical_payload_digest
from shared.remote_access import PrincipalCredentialRequired, TerminalConnection
from workspaces.services import WorkspaceOperation

from ._range_workspace import authorize_range_workspace

if TYPE_CHECKING:
    from uuid import UUID

    from django.contrib.auth.models import User

    from engine.services import RangeAuthorityProjection
    from shared.credentials import CredentialContext
    from shared.remote_access import TerminalConnectionFactory


_RANGE_ACCESS = "range.access"


_INSTANCE_NOT_FOUND = "Instance not found"


def admit_range_remote_authority(
    credential: CredentialContext, instance_uuid: str, *, range_uuid: UUID | None = None
) -> dict[str, object]:
    """Capture non-secret facts only after the owning services authorize the guest."""
    from engine.services import project_range_authority

    from ._unified_authorization import require_range_action

    current = resolve_current_credential(credential)
    request_id = _owned_request_id(current, instance_uuid, range_uuid=range_uuid)
    row = RangeInstance.objects.filter(request_id=request_id).first() if request_id is not None else None
    if row is None:
        raise ValueError(_INSTANCE_NOT_FOUND)
    require_range_action(current, row.uuid, _RANGE_ACCESS)
    projection = project_range_authority(row.request.request_id)
    if projection.resource_generation is None:
        raise ValueError(_INSTANCE_NOT_FOUND)
    return {**credential_reference_fields(current), **_range_remote_authority_fields(projection, instance_uuid)}


def _range_remote_authority_fields(projection: RangeAuthorityProjection, instance_uuid: str) -> dict[str, object]:
    """Build the same immutable remote binding for admission and locked readback."""
    scope, parent = projection.scope, projection.parent
    binding = canonical_payload_digest(
        {
            "target": instance_uuid,
            "range": str(projection.public_uuid),
            "runtime": str(projection.native_uuid),
            "generation": str(projection.resource_generation),
            "request": str(projection.request_uuid),
            "owner": str(projection.owner_principal_uuid),
            "account": str(scope.account_uuid),
            "organization": str(scope.organization_uuid),
            "workspace": str(scope.workspace_uuid),
            "parent_type": parent.type,
            "parent": str(parent.uuid),
        }
    )
    return {
        "authority_kind": "principal",
        "authority_action": _RANGE_ACCESS,
        "authority_target_type": "range",
        "authority_target_uuid": projection.public_uuid,
        "authority_parent_type": parent.type,
        "authority_parent_uuid": parent.uuid,
        "authority_scope_kind": scope.kind,
        "authority_account_uuid": scope.account_uuid,
        "authority_organization_uuid": scope.organization_uuid,
        "authority_workspace_uuid": scope.workspace_uuid,
        "authority_owner_uuid": projection.owner_principal_uuid,
        "authority_resource_uuid": projection.native_uuid,
        "authority_binding_digest": binding,
    }


def assert_range_remote_authority_binding(authority: dict[str, object], member: str) -> None:
    """Fence CMS and Engine custody for one previously admitted remote handoff."""
    from engine.services import lock_range_remote_authority

    from ._unified_authorization import _intent_scope_binding, _require_realized_binding

    instance = RangeInstance.objects.select_for_update(of=("self",)).get(
        uuid=authority["authority_target_uuid"], status="ready"
    )
    instance.request = Request.objects.select_for_update().get(pk=instance.request_id)
    projection = lock_range_remote_authority(instance.uuid, member)
    scope, parent = _intent_scope_binding(instance)
    _require_realized_binding(instance, scope, parent, read_only_intent=False)
    if projection.resource_generation is None or any(
        authority.get(name) != value for name, value in _range_remote_authority_fields(projection, member).items()
    ):
        raise ValueError("Range remote binding changed")


def _owned_request_id(
    user: User | CredentialContext, instance_uuid: str, *, range_uuid: UUID | None = None
) -> int | None:
    """Return the request id owning ``instance_uuid``, across both registries.

    Interactive access spans two instance registries and must consult both:

    * realized **range** instances are rows of ``engine.models.Instance``,
      written by the engine interpreter during provisioning, and are reachable
      only through ``engine.services`` (ADR-001);
    * **NGFW** instances are rows of this layer's own ``Instance`` model,
      written by ``cms.services._ngfw_provisioning``.

    Consulting only the CMS table (the original behavior) matched nothing for a
    range instance, so every range terminal/RDP/SSH open failed closed with
    "Instance not found". Consulting only the engine table would break NGFW
    access the same way. Ownership is enforced in both branches.
    """
    from engine.services import get_owned_instance_request_ref

    request_ref = get_owned_instance_request_ref(
        user, instance_uuid, **({"range_uuid": range_uuid} if range_uuid else {})
    )
    if request_ref is not None:
        # Join on the shared ``request_id`` UUID: engine and CMS keep separate
        # request tables whose primary keys are not a contract.
        return Request.objects.filter(request_id=request_ref).values_list("id", flat=True).first()
    if isinstance(user, CredentialContext):
        return None
    ngfw_instance = (
        Instance.objects.filter(
            pk=instance_uuid, request__user_id=getattr(user, "id", None), request__owner_principal_uuid__isnull=True
        )
        .values_list("request_id", flat=True)
        .first()
    )
    return ngfw_instance


def _authorize_instance_access(
    user: User | CredentialContext, instance_uuid: str, *, range_uuid: UUID | None = None
) -> None:
    """Enforce ownership and the persisted request workspace binding.

    Ownership resolves through ``engine.services`` rather than ``cms.models``:
    realized range instances are rows of ``engine.models.Instance``, while the
    CMS ``Instance`` table is written only by NGFW provisioning. Resolving the
    owner here against the CMS table matched nothing for a range instance, so
    every terminal/RDP/SSH open failed with "Instance not found" (the workspace
    check below was never reached). The engine seam enforces the ownership half;
    the workspace binding is read from this layer's own ``Request`` row, which is
    where ``workspace_id`` is recorded.
    """
    try:
        request_id = _owned_request_id(user, instance_uuid, range_uuid=range_uuid)
    except (ValidationError, ValueError) as exc:
        raise ValueError(_INSTANCE_NOT_FOUND) from exc
    if request_id is None:
        raise ValueError(_INSTANCE_NOT_FOUND)
    if isinstance(user, CredentialContext):
        from ._unified_authorization import require_range_action

        row = RangeInstance.objects.filter(request_id=request_id).first()
        if row is None:
            raise ValueError(_INSTANCE_NOT_FOUND)
        require_range_action(user, row.uuid, _RANGE_ACCESS)
        return
    workspace_id = Request.objects.filter(pk=request_id).values_list("workspace_id", flat=True).first()
    try:
        authorize_range_workspace(user, workspace_id, WorkspaceOperation.ACCESS_RANGE)
    except CMSError as exc:
        raise PermissionError(_INSTANCE_NOT_FOUND) from exc


def _terminal_actor(
    user: User | CredentialContext, instance_uuid: str, credential: CredentialContext | None
) -> User | CredentialContext:
    """Use the live browser proof on principal resources; preserve legacy admission."""
    if credential is None or isinstance(user, CredentialContext):
        return user
    from shared.principal_port import principal_for_user

    current = resolve_current_credential(credential)
    if principal_for_user(user) != current.principal:
        raise PermissionError(_INSTANCE_NOT_FOUND)
    try:
        legacy_request = _owned_request_id(user, instance_uuid)
    except PrincipalCredentialRequired:
        return current
    return user if legacy_request is not None else current


def authorize_range_terminal(
    user: User | CredentialContext,
    instance_uuid: str,
    *,
    range_uuid: UUID | None = None,
    credential: CredentialContext | None = None,
) -> None:
    """Recheck ongoing guest access without loading credentials or opening I/O."""
    _authorize_instance_access(_terminal_actor(user, instance_uuid, credential), instance_uuid, range_uuid=range_uuid)


def connect_range_terminal(
    user: User | CredentialContext,
    instance_uuid: str,
    *,
    connection_factory: TerminalConnectionFactory | None = None,
    range_uuid: UUID | None = None,
    credential: CredentialContext | None = None,
) -> TerminalConnection:
    """Open an Engine terminal only after workspace authorization succeeds.

    ``connection_factory`` is carried through to ``engine.services`` so a
    consumer test can substitute a fake terminal transport without bypassing
    this workspace authorization layer or Engine's runtime authorization
    (issue #993). Production leaves it ``None`` (a real SSH connection).
    """
    from engine.services import connect_terminal

    user = _terminal_actor(user, instance_uuid, credential)
    _authorize_instance_access(user, instance_uuid, range_uuid=range_uuid)
    return connect_terminal(
        user, instance_uuid, connection_factory=connection_factory, **({"range_uuid": range_uuid} if range_uuid else {})
    )


def get_range_rdp_connection_info(
    user: User | CredentialContext, instance_uuid: str, *, range_uuid: UUID | None = None
) -> dict[str, Any]:
    """Resolve RDP data only after workspace authorization succeeds."""
    from engine.services import get_rdp_connection_info

    _authorize_instance_access(user, instance_uuid, range_uuid=range_uuid)
    return get_rdp_connection_info(user, instance_uuid, **({"range_uuid": range_uuid} if range_uuid else {}))


def get_range_ssh_connection_info(
    user: User | CredentialContext, instance_uuid: str, *, range_uuid: UUID | None = None
) -> dict[str, Any]:
    """Resolve SSH data only after workspace authorization succeeds."""
    from engine.services import get_ssh_connection_info

    _authorize_instance_access(user, instance_uuid, range_uuid=range_uuid)
    return get_ssh_connection_info(user, instance_uuid, **({"range_uuid": range_uuid} if range_uuid else {}))
