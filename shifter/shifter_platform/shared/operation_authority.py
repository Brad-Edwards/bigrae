"""Non-secret credential references composed into incumbent operation records."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal, cast

if TYPE_CHECKING:
    from shared.db.operation_authority import RecordedAuthorityFields

from collections.abc import Mapping
from uuid import UUID

from shared.api_tokens.scopes import validate_scopes
from shared.authorization import CredentialCeiling, TargetRef
from shared.authorization.catalog import TargetType
from shared.credentials import CredentialContext
from shared.identity_scope import PrincipalRef, ResourceScope

_OPERATION_AUTHORITY_UNAVAILABLE = "Operation authority unavailable"


def authority_resource_scope(fields: Mapping[str, object]) -> ResourceScope:
    """Decode the incumbent scalar scope columns with the shared shape contract."""
    identities = []
    for name in ("authority_account_uuid", "authority_organization_uuid", "authority_workspace_uuid"):
        value = fields[name]
        if value is not None and not isinstance(value, UUID):
            raise ValueError("Operation scope unavailable")
        identities.append(value)
    account, organization, workspace = identities
    if fields["authority_scope_kind"] == "account":
        return ResourceScope("account", account, organization, workspace)
    if fields["authority_scope_kind"] == "installation":
        return ResourceScope("installation", account, organization, workspace)
    raise ValueError("Operation scope unavailable")


def credential_reference_fields(credential: CredentialContext) -> dict[str, object]:
    """Store registered actions/scopes and exact reference, never bearer proof."""
    target = credential.ceiling.target
    scopes = validate_scopes(sorted(credential.scopes)) if credential.scopes else []
    return {
        "initiating_principal_uuid": credential.principal.uuid,
        "initiating_principal_kind": credential.principal.kind,
        "credential_kind": credential.kind,
        "credential_uuid": credential.credential_uuid,
        "credential_ceiling_actions": "\n".join(sorted(credential.ceiling.actions)),
        "credential_target_type": target.type if target else "",
        "credential_target_uuid": target.uuid if target else None,
        "credential_scopes": "\n".join(sorted(scopes)),
        "credential_event_uuid": credential.event_uuid,
    }


def _registered_lines(value: str) -> frozenset[str]:
    """Parse canonical registered values without admitting arbitrary scope material."""
    if not isinstance(value, str):
        raise ValueError(_OPERATION_AUTHORITY_UNAVAILABLE)
    parts = frozenset(value.splitlines()) if value else frozenset()
    if "\n".join(sorted(parts)) != value:
        raise ValueError(_OPERATION_AUTHORITY_UNAVAILABLE)
    return parts


def credential_reference_from_record(row: RecordedAuthorityFields) -> CredentialContext:
    """Reconstruct only a trusted server-owned record, never request fields."""
    try:
        target = (
            TargetRef(cast(TargetType, row.credential_target_type), row.credential_target_uuid)
            if row.credential_target_type
            else None
        )
        if target is None and row.credential_target_uuid is not None:
            raise ValueError("Invalid target")
        scopes = _registered_lines(row.credential_scopes)
        if scopes:
            validate_scopes(sorted(scopes))
        if row.initiating_principal_uuid is None or row.credential_uuid is None:
            raise ValueError("Credential identity unavailable")
        return CredentialContext(
            PrincipalRef(
                row.initiating_principal_uuid, cast(Literal["human", "service"], row.initiating_principal_kind)
            ),
            cast(Literal["session", "personal", "service", "temporary"], row.credential_kind),
            row.credential_uuid,
            CredentialCeiling(_registered_lines(row.credential_ceiling_actions), target),
            scopes,
            row.credential_event_uuid,
        )
    except Exception:  # noqa: BLE001  # fail-closed authority boundary; the cause stays out of the chain
        raise ValueError(_OPERATION_AUTHORITY_UNAVAILABLE) from None
