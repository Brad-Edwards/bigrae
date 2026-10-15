"""Non-secret credential references composed into incumbent operation records."""

from shared.api_tokens.scopes import validate_scopes
from shared.authorization import CredentialCeiling, TargetRef
from shared.credentials import CredentialContext
from shared.identity_scope import PrincipalRef


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
    if not isinstance(value, str):
        raise ValueError("Operation authority unavailable")
    parts = frozenset(value.splitlines()) if value else frozenset()
    if "\n".join(sorted(parts)) != value:
        raise ValueError("Operation authority unavailable")
    return parts


def credential_reference_from_record(row) -> CredentialContext:
    """Reconstruct only a trusted server-owned record, never request fields."""
    try:
        target = (
            TargetRef(row.credential_target_type, row.credential_target_uuid) if row.credential_target_type else None
        )
        if target is None and row.credential_target_uuid is not None:
            raise ValueError("Invalid target")
        scopes = _registered_lines(row.credential_scopes)
        if scopes:
            validate_scopes(sorted(scopes))
        return CredentialContext(
            PrincipalRef(row.initiating_principal_uuid, row.initiating_principal_kind),
            row.credential_kind,
            row.credential_uuid,
            CredentialCeiling(_registered_lines(row.credential_ceiling_actions), target),
            scopes,
            row.credential_event_uuid,
        )
    except Exception:
        raise ValueError("Operation authority unavailable") from None
