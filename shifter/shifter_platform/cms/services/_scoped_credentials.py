"""S6 branch of the incumbent credential service, activated with S8 callers."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from typing import Any

from django.db import transaction
from django.utils import timezone

from cms.exceptions import CMSError
from cms.models import Credential, CredentialType
from shared.audit import AuditAction, AuditEntityType, AuditEvent, audit_log, principal_actor_fields
from shared.credentials import CredentialContext
from shared.identity_scope import ResourceScope
from shared.schemas.credentials import CredentialRef, DeploymentProfileContext, SCMCredentialContext

from ._content_authority import content_scope, content_scope_fields, fence_content_scope, require_content_scope
from ._credential_input import validate_credential_data

_CONTENT_UNAVAILABLE = "Content unavailable"


def _projection(row: Credential) -> SCMCredentialContext | DeploymentProfileContext:
    """Use the existing display contracts; secret fields never leave CMS."""
    common = {
        "credential_id": row.pk,
        "name": row.name,
        "user_id": row.user_id,
        "owner_principal_uuid": row.owner_principal_uuid,
        "created_at": row.created_at,
        "expires_at": row.expires_at,
        "is_deleted": row.is_deleted,
    }
    if row.credential_type.slug == "scm":
        return SCMCredentialContext.model_validate(
            {
                **common,
                "scm_folder_name": row.data.get("scm_folder_name", ""),
                "scm_pin_id": row.data.get("scm_pin_id", ""),
                "sls_region": row.data.get("sls_region", ""),
            }
        )
    if row.credential_type.slug == "deployment_profile":
        code = row.data.get("authcode", "")
        masked = f"{code[:5]}***" if len(code) >= 5 else "***"
        return DeploymentProfileContext.model_validate({**common, "authcode_masked": masked})
    raise CMSError(_CONTENT_UNAVAILABLE)


def _reference(row: Credential) -> CredentialRef:
    """Project public credential identity without returning secret fields."""
    return CredentialRef(
        credential_id=row.pk,
        user_id=row.user_id,
        owner_principal_uuid=row.owner_principal_uuid,
        is_deleted=row.is_deleted,
    )


def _record(credential: CredentialContext, row: Credential, action: str) -> None:
    """Audit a credential operation with the admitted principal identity."""
    audit_log(
        AuditEvent(
            entity_type=AuditEntityType.CREDENTIAL,
            entity_id=row.pk,
            action=action,
            **principal_actor_fields(credential.principal),
            new_state={"credential_type": row.credential_type.slug, "scope_generation": row.scope_generation},
        ),
        strict=True,
    )


def create_scoped_credential(
    credential: CredentialContext, slug: str, scope: ResourceScope, values: dict[str, Any]
) -> CredentialRef:
    """Retain encryption and model validation, with principal-owned attribution."""
    current = require_content_scope(credential, scope, "manage_credentials")
    kind = CredentialType.objects.filter(slug=slug).first()
    if kind is None:
        raise CMSError(_CONTENT_UNAVAILABLE)
    data = dict(values)
    name = data.pop("name", None)
    expires_at = data.pop("expires_at", None)
    if not isinstance(name, str) or not name.strip():
        raise ValueError("name is required")
    if slug == "scm" and not data.get("scm_folder_name"):
        data["scm_folder_name"] = ""
    data = validate_credential_data(
        kind.spec_slug,
        data,
        name=name,
        expires_at=expires_at,
        user_id=None,
        owner_principal_uuid=current.principal.uuid,
    )
    # Validation cannot turn an admission snapshot into continuing authority.
    # Recheck the same proof and scope before persisting encrypted secret data.
    current = require_content_scope(current, scope, "manage_credentials")
    row = Credential(
        user=None,
        owner_principal_uuid=current.principal.uuid,
        **content_scope_fields(scope),
        name=name,
        credential_type=kind,
        expires_at=expires_at,
        data=data,
    )
    with transaction.atomic():
        fence_content_scope(current, scope)
        resolve_scope = content_scope(row)
        if resolve_scope != scope:
            raise CMSError(_CONTENT_UNAVAILABLE)
        row.full_clean()
        row.save()
        _record(current, row, AuditAction.CREATE)
    return _reference(row)


def _authorized_row(
    credential: CredentialContext, identifier: int, operation: str
) -> tuple[CredentialContext, dict[str, object]]:
    """Admit the selected credential before decrypting its private fields."""
    row = Credential.objects.defer("data").select_related("credential_type").filter(pk=identifier).first()
    if row is None:
        raise CMSError(_CONTENT_UNAVAILABLE)
    scope = content_scope(row)
    current = require_content_scope(credential, scope, operation)
    fence = {
        "pk": row.pk,
        "owner_principal_uuid": row.owner_principal_uuid,
        "scope_generation": row.scope_generation,
        **content_scope_fields(scope),
    }
    return current, fence


def get_scoped_credential(
    credential: CredentialContext, identifier: int
) -> SCMCredentialContext | DeploymentProfileContext:
    """Return the safe display projection of an authorized credential."""
    _, fence = _authorized_row(credential, identifier, "read_content")
    row = Credential.objects.select_related("credential_type").filter(**fence).first()
    if row is None:
        raise CMSError(_CONTENT_UNAVAILABLE)
    return _projection(row)


def list_scoped_credentials(
    credential: CredentialContext, scope: ResourceScope | None
) -> list[SCMCredentialContext | DeploymentProfileContext]:
    """Filter credential custody before decrypting and projecting rows."""
    if scope is None:
        raise CMSError(_CONTENT_UNAVAILABLE)
    require_content_scope(credential, scope, "read_content")
    # Filter before decrypting data, projecting, counting, or paginating.
    rows = Credential.objects.defer("data").filter(**content_scope_fields(scope), owner_principal_uuid__isnull=False)
    result = []
    for row in rows.select_related("credential_type").order_by("-created_at"):
        content_scope(row)
        result.append(_projection(row))
    return result


def delete_scoped_credential(credential: CredentialContext, identifier: int) -> CredentialRef:
    """Retire a credential under its original admitted scope and owner."""
    current, fence = _authorized_row(credential, identifier, "manage_credentials")
    with transaction.atomic():
        row = Credential.objects.select_for_update().select_related("credential_type").filter(**fence).first()
        if row is None:
            raise CMSError(_CONTENT_UNAVAILABLE)
        fence_content_scope(current, content_scope(row))
        row.deleted_at = timezone.now()
        row.save(update_fields=["deleted_at"])
        _record(current, row, AuditAction.DELETE)
    return _reference(row)
