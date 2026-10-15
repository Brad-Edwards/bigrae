"""CMS composition of range access and CTF's participant decision for VPN."""

from shared.credentials import resolve_current_credential


def admit_range_vpn_authority(credential, request_id, *, participation: bool = False):
    from cms.models import RangeInstance
    from engine.services import project_range_authority

    from ._unified_authorization import _range_binding, require_range_action

    current = resolve_current_credential(credential)
    instance = RangeInstance.objects.select_related("request").get(request__request_id=request_id)
    before, scope, parent = _range_binding(instance.uuid)
    snapshot = (before.owner_principal_uuid, before.request_id, before.range_id, scope, parent)
    if current.kind == "temporary" or participation:
        from shared.range_workspace_aggregate import require_range_vpn_participation

        require_range_vpn_participation(current, instance.request.request_id, instance.event_uuid)
        instance, scope, parent = _range_binding(instance.uuid)
        action, target = "event.participate", parent
    else:
        instance = require_range_action(current, instance.uuid, "range.access")
        instance, scope, parent = _range_binding(instance.uuid)
        from shared.authorization import TargetRef

        action, target = "range.access", TargetRef("range", instance.uuid)
    projection = project_range_authority(request_id)
    if (instance.owner_principal_uuid, instance.request_id, instance.range_id, scope, parent) != snapshot:
        raise ValueError("VPN range changed")
    return {
        "authority_kind": "principal",
        "authority_action": action,
        "authority_target_type": target.type,
        "authority_target_uuid": target.uuid,
        "authority_parent_type": parent.type,
        "authority_parent_uuid": parent.uuid,
        "authority_scope_kind": scope.kind,
        "authority_account_uuid": scope.account_uuid,
        "authority_organization_uuid": scope.organization_uuid,
        "authority_workspace_uuid": scope.workspace_uuid,
        "authority_owner_uuid": projection.owner_principal_uuid,
        "authority_resource_uuid": instance.uuid,
    }
