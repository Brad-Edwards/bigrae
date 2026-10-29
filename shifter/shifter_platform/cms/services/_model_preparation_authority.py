"""Current native proof and exact retained owner admission for model preparation."""

from __future__ import annotations

from cms.models import RangeInstance
from shared.authorization import TargetRef
from shared.credentials import CredentialContext, resolve_current_credential
from shared.model_access import ContractError, OwnedReference
from shared.model_access.reservation import SystemPreparationAuthority, WarmPreparationAuthority
from shared.operation_authority import credential_reference_from_record

_AUTHORITY_UNAVAILABLE = "allocation.authority_unavailable"


def _owner_reference(instance: RangeInstance) -> OwnedReference:
    """Use the durable principal identity; a contact user is never authority."""
    if instance.owner_principal_uuid is None:
        raise ContractError(_AUTHORITY_UNAVAILABLE)
    return OwnedReference(owner="management", reference=f"principal:{instance.owner_principal_uuid}")


def _authorize_preparation(
    instance: RangeInstance,
    owner: OwnedReference,
    system: SystemPreparationAuthority | WarmPreparationAuthority | None,
    *,
    credential: CredentialContext | None = None,
    renew: bool = False,
    authority_action: str = "range.manage",
    authority_target: TargetRef | None = None,
) -> CredentialContext:
    """Revalidate the exact initiating credential before delayed provider work."""
    assert instance.request is not None
    try:
        if renew:
            if credential is None:
                raise ContractError(_AUTHORITY_UNAVAILABLE)
            current = resolve_current_credential(credential)
            from ._unified_authorization import require_range_action

            require_range_action(current, instance.uuid, authority_action, authority_target=authority_target)
        else:
            current = resolve_current_credential(credential_reference_from_record(instance.request))
        if not renew and current.principal.uuid != instance.owner_principal_uuid:
            _require_original_event_authority(instance, current)
        if system is not None and not _system_preparation_matches(instance, owner, system, current):
            raise ContractError("allocation.system_owner_unavailable")
        return current
    except Exception:  # noqa: BLE001  # fail-closed authority boundary; suppress private causes
        raise ContractError(_AUTHORITY_UNAVAILABLE) from None


def _require_original_event_authority(instance: RangeInstance, current: CredentialContext) -> None:
    """A retained organizer proof may act for a participant only through its exact event."""
    from shared.authorization import AuthorizationRequest, configured_authorization_provider
    from workspaces.services import resource_scope_from_ids

    if instance.range_source != "ctf" or instance.event_uuid is None:
        raise ContractError(_AUTHORITY_UNAVAILABLE)
    scope = resource_scope_from_ids(
        kind=instance.scope_kind,
        account_id=instance.account_id,
        organization_id=instance.organization_id,
        workspace_id=instance.workspace_id,
    )
    decision = configured_authorization_provider().check(
        AuthorizationRequest(
            current.principal,
            "event.manage_ranges",
            TargetRef("event", instance.event_uuid),
            scope,
            current.ceiling,
        )
    )
    if not decision.allowed:
        raise ContractError(_AUTHORITY_UNAVAILABLE)


def _system_preparation_matches(
    instance: RangeInstance,
    owner: OwnedReference,
    system: SystemPreparationAuthority | WarmPreparationAuthority,
    current: CredentialContext,
) -> bool:
    """Retained system preparation admits only exact warm or event-spare custody."""
    assert instance.request is not None
    if system.owner_ref != owner:
        return False
    if system.kind == "warm_pool":
        return (
            current.principal.kind,
            instance.user_id,
            instance.request.user_id,
            instance.scope_kind,
            instance.request.scope_kind,
            instance.account_id,
            instance.organization_id,
            instance.workspace_id,
            instance.request.account_id,
            instance.request.organization_id,
            instance.request.workspace_id,
        ) == ("service", None, None, "installation", "installation", None, None, None, None, None, None)
    return (
        system.kind == "ctf_spare"
        and instance.range_source == "ctf"
        and instance.event_uuid is not None
        and instance.request.scope_kind == instance.scope_kind == "account"
    )
