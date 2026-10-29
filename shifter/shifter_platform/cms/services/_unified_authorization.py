"""CMS-owned S6 range/intent projection and live common-action decisions."""

from uuid import UUID

from cms.exceptions import CMSError
from cms.models import RangeInstance
from engine.services import has_range_runtime, project_range_authority
from shared.authorization import (
    AuthorizationProviderBindingError,
    AuthorizationRequest,
    TargetRef,
    resolve_authorization_event_scope,
)
from shared.authorization.policy import require_authorization, scope_parent
from shared.credentials import CredentialContext, resolve_current_credential
from shared.identity_scope import ResourceScope
from shared.range_workspace_aggregate import range_event_parent
from workspaces.services import resource_scope_from_ids

_UNAVAILABLE = "Range unavailable"


def _range_binding(
    range_uuid: UUID, *, include_terminal: bool = False, read_only_intent: bool = False
) -> tuple[RangeInstance, ResourceScope, TargetRef]:
    """Resolve intent, runtime and actual aggregate identity before policy."""
    instance = (
        RangeInstance.all_objects.defer("range_spec", "model_launch_scope", "model_sources", "model_source_sponsorship")
        .select_related("request")
        .filter(uuid=range_uuid)
        .first()
    )
    if (
        instance is None
        or (instance.is_deleted and not include_terminal)
        or instance.request is None
        or instance.request.request_type != "range"
        or instance.owner_principal_uuid is None
    ):
        raise ValueError(_UNAVAILABLE)
    if not isinstance(instance.owner_principal_uuid, UUID) or not instance.owner_principal_uuid.int:
        raise ValueError(_UNAVAILABLE)
    scope, parent = _intent_scope_binding(instance)
    _require_realized_binding(instance, scope, parent, read_only_intent=read_only_intent)
    return instance, scope, parent


def _intent_scope_binding(instance: RangeInstance) -> tuple[ResourceScope, TargetRef]:
    """Resolve the intent's recorded request custody and any immutable event parent."""
    if instance.request is None:
        raise ValueError(_UNAVAILABLE)
    request = instance.request
    fields = ("scope_kind", "account_id", "organization_id", "workspace_id", "owner_principal_uuid")
    if any(getattr(instance, field) != getattr(request, field) for field in fields):
        raise ValueError(_UNAVAILABLE)
    scope = resource_scope_from_ids(
        kind=instance.scope_kind,
        account_id=instance.account_id,
        organization_id=instance.organization_id,
        workspace_id=instance.workspace_id,
    )
    if scope.kind != "account":
        raise ValueError(_UNAVAILABLE)
    event = range_event_parent(request.request_id, instance.pk)
    if event != instance.event_uuid or (event is not None and resolve_authorization_event_scope(event) != scope):
        raise ValueError(_UNAVAILABLE)
    parent = TargetRef("event", event) if event is not None else scope_parent(scope)
    return scope, parent


def _require_realized_binding(
    instance: RangeInstance, scope: ResourceScope, parent: TargetRef, *, read_only_intent: bool
) -> None:
    """Require matching runtime custody except for an unrealized failed-intent read."""
    if instance.request is None:
        raise ValueError(_UNAVAILABLE)
    request = instance.request
    if (
        read_only_intent
        and instance.status == "failed"
        and instance.range_id is None
        and not has_range_runtime(request.request_id, instance.uuid)
    ):
        return
    realized = project_range_authority(request.request_id)
    if (realized.public_uuid, realized.native_id, realized.scope, realized.owner_principal_uuid, realized.parent) != (
        instance.uuid,
        instance.range_id,
        scope,
        instance.owner_principal_uuid,
        parent,
    ):
        raise ValueError(_UNAVAILABLE)


def range_authorization_binding(
    range_uuid: UUID, *, include_terminal: bool = False, read_only_intent: bool = False
) -> tuple[TargetRef, ResourceScope]:
    """Project the exact leaf/parent binding for policy and durable work."""
    try:
        _, scope, parent = _range_binding(
            range_uuid, include_terminal=include_terminal, read_only_intent=read_only_intent
        )
        return parent, scope
    except Exception:  # noqa: BLE001  # fail-closed authority boundary; the cause stays out of the chain
        raise ValueError(_UNAVAILABLE) from None


def require_range_action(
    credential: CredentialContext,
    range_uuid: UUID,
    action: str,
    *,
    include_terminal: bool = False,
    authority_target: TargetRef | None = None,
) -> RangeInstance:
    """Authorize direct callers without creator/contact or human-only vetoes.

    Event-owned ranges may use the exact event range-management grant admitted
    by the CTF domain. Generic callers continue to require the range leaf.
    """
    try:
        current = resolve_current_credential(credential)
        instance, scope, parent = _range_binding(
            range_uuid, include_terminal=include_terminal, read_only_intent=action == "range.read"
        )
        if (
            include_terminal
            and action != "range.read"
            and (
                action not in {"range.manage", "event.manage_ranges"}
                or (instance.is_deleted and instance.status not in {"destroying", "failed"})
            )
        ):
            raise ValueError(_UNAVAILABLE)
        target = TargetRef("range", instance.uuid)
        request_parent: TargetRef | None = parent
        if authority_target is not None:
            if action != "event.manage_ranges" or parent.type != "event" or authority_target != parent:
                raise ValueError(_UNAVAILABLE)
            target = authority_target
            request_parent = None
        request = AuthorizationRequest(current.principal, action, target, scope, current.ceiling, parent=request_parent)
        require_authorization(request)
        return instance
    except AuthorizationProviderBindingError:
        raise CMSError("Range authority unavailable", details={"code": "authority-unavailable"}) from None
    except Exception:  # noqa: BLE001  # fail-closed authority boundary; the cause stays out of the chain
        raise CMSError(_UNAVAILABLE) from None
