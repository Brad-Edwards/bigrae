"""Initiating authority on the incumbent preparation operation and its attempts."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

from shared.authorization import AuthorizationRequest
from shared.authorization.policy import require_authorization, scope_parent
from shared.credentials import CredentialContext, resolve_credential_scope, resolve_current_credential
from shared.exceptions import ValidationError
from shared.identity_scope import ResourceScope
from shared.operation_authority import (
    authority_resource_scope,
    credential_reference_fields,
    credential_reference_from_record,
)
from shared.operation_envelope import canonical_payload_digest

if TYPE_CHECKING:
    from uuid import UUID

    from engine.models import PreparationOperation
    from shared.credentials import CredentialContext
    from shared.identity_scope import ResourceScope


_ARTIFACT_PREPARATION_AUTHORITY_UNAVAILABLE = "Artifact preparation authority unavailable"


def admit_preparation_authority(credential: CredentialContext, scope: ResourceScope) -> dict[str, object]:
    """Check the existing parent before creating an operation identity."""
    try:
        current = resolve_current_credential(credential)
        target = scope_parent(scope)
        if resolve_credential_scope(target) != scope:
            raise ValueError("Scope changed")
        action = f"{target.type}.prepare_content"
        require_authorization(AuthorizationRequest(current.principal, action, target, scope, current.ceiling))
        return {
            **credential_reference_fields(current),
            "authority_kind": "principal",
            "authority_action": action,
            "authority_target_type": target.type,
            "authority_target_uuid": target.uuid,
            "authority_scope_kind": scope.kind,
            "authority_account_uuid": scope.account_uuid,
            "authority_organization_uuid": scope.organization_uuid,
            "authority_workspace_uuid": scope.workspace_uuid,
            "authority_owner_uuid": current.principal.uuid,
        }
    except Exception:  # noqa: BLE001  # fail-closed authority check; every failure is one bounded denial
        raise ValidationError(_ARTIFACT_PREPARATION_AUTHORITY_UNAVAILABLE) from None


def preparation_authority_digest(fields: dict[str, object]) -> str:
    """Digest the immutable preparation admission and initiating proof reference."""
    return canonical_payload_digest(
        {key: str(value) if isinstance(value, UUID) else value for key, value in fields.items()}
    )


def authorize_preparation_credential(credential: CredentialContext, operation: PreparationOperation | None) -> None:
    """Administrators act on the actual scope without a human-requester veto."""
    if operation is None or operation.authority_kind != "principal":
        raise ValidationError("The artifact preparation is unavailable")
    scope = authority_resource_scope(
        {
            name: getattr(operation, name)
            for name in (
                "authority_scope_kind",
                "authority_account_uuid",
                "authority_organization_uuid",
                "authority_workspace_uuid",
            )
        }
    )
    admit_preparation_authority(credential, scope)
    verify_preparation_authority_binding(operation)


def reauthorize_preparation_initiator(operation: PreparationOperation) -> None:
    """A retry or input read retains the original proof and exact ceiling."""
    try:
        authorize_preparation_credential(credential_reference_from_record(operation), operation)
    except Exception:  # noqa: BLE001  # fail-closed authority check; every failure is one bounded denial
        raise ValidationError(_ARTIFACT_PREPARATION_AUTHORITY_UNAVAILABLE) from None


def verify_preparation_authority_binding(operation: PreparationOperation) -> None:
    """A pure persisted-fact fence, including during independent cleanup."""
    fields = credential_reference_fields(credential_reference_from_record(operation))
    for name in (
        "authority_kind",
        "authority_action",
        "authority_target_type",
        "authority_target_uuid",
        "authority_scope_kind",
        "authority_account_uuid",
        "authority_organization_uuid",
        "authority_workspace_uuid",
        "authority_owner_uuid",
    ):
        fields[name] = getattr(operation, name)
    if operation.authority_kind != "principal" or operation.input.get(
        "authority_digest"
    ) != preparation_authority_digest(fields):
        raise ValidationError(_ARTIFACT_PREPARATION_AUTHORITY_UNAVAILABLE)
