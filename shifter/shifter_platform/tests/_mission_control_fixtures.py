"""Real native ownership and explicit action ceilings for retained MC tests."""

from uuid import uuid4

from cms.models import RangeInstance, Request
from shared.enums import RequestType


def native_scopes(granted):
    mapping = {
        "mission_control:range:read": ("range.read",),
        "mission_control:range:write": (
            "range.manage",
            "account.launch_range",
            "organization.launch_range",
            "workspace.launch_range",
            "account.read_content",
            "organization.read_content",
            "workspace.read_content",
        ),
        "mission_control:upload:write": (
            "account.manage_content",
            "organization.manage_content",
            "workspace.manage_content",
        ),
        "mission_control:ngfw:read": ("account.read_devices", "organization.read_devices", "workspace.read_devices"),
        "mission_control:ngfw:write": (
            "account.manage_devices",
            "organization.manage_devices",
            "workspace.manage_devices",
        ),
        "mission_control:guacamole:read": (
            "range.access",
            "account.access_devices",
            "organization.access_devices",
            "workspace.access_devices",
        ),
        "mission_control:credentials:write": (
            "account.manage_credentials",
            "organization.manage_credentials",
            "workspace.manage_credentials",
        ),
    }
    return sorted(
        {"authorization:" + action for scope in granted for action in mapping.get(scope, ())}
        | {scope for scope in granted if scope not in mapping}
    )


def individual_account(user):
    from shared.principal_port import principal_for_user
    from workspaces.models import Account
    from workspaces.services import create_account

    principal = principal_for_user(user)
    return Account.objects.filter(individual_principal_uuid=principal.uuid).first() or create_account(
        kind="individual", name="Synthetic native account", owner=principal
    )


def native_range_for_user(user):
    """Create a RangeInstance + Request owned by ``user``; return the request_id."""
    from engine.models import Range as EngineRange
    from engine.models import Request as EngineRequest
    from shared.principal_port import principal_for_user
    from workspaces.models import Account
    from workspaces.services import create_account

    principal = principal_for_user(user)
    account = Account.objects.filter(
        kind=Account.Kind.INDIVIDUAL, individual_principal_uuid=principal.uuid
    ).first() or create_account(kind=Account.Kind.INDIVIDUAL, name="Individual account", owner=principal)
    custody = {
        "owner_principal_uuid": principal.uuid,
        "scope_kind": "account",
        "account_id": account.id,
    }
    request = Request.objects.create(request_id=uuid4(), request_type=RequestType.RANGE.value, user=user, **custody)
    instance = RangeInstance.objects.create(
        scenario_id="basic", user_id=user.id, status="ready", request=request, **custody
    )
    engine_request = EngineRequest.objects.create(
        request_id=request.request_id,
        request_type=RequestType.RANGE.value,
        user=user,
        **custody,
    )
    realized = EngineRange.objects.create(
        request=engine_request,
        user=user,
        cms_user_id=user.pk,
        cms_range_uuid=instance.uuid,
        resource_generation=uuid4(),
        range_backend="gce",
        status="ready",
        **custody,
    )
    instance.range_id = realized.pk
    instance.save(update_fields=["range_id"])
    return request.request_id
