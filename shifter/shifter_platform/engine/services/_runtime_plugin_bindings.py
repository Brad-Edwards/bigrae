"""Bind tenant-owned plugins to exact pack digests and retain range selections."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from uuid import UUID

from django.db import transaction
from shifter_adapter_sdk.runtime import PluginManifest, canonical_digest

from shared.audit import (
    AuditAction,
    AuditEntityType,
    AuditEvent,
    RequestAudit,
    audit_log,
    principal_actor_fields,
)
from shared.authorization import TargetRef
from shared.credentials import (
    CredentialContext,
    resolve_credential_scope,
    resolve_local_credential,
)
from shared.exceptions import ValidationError
from shared.identity_scope import ResourceScope
from shared.runtime_plugin_binding import PluginTargetBindings, RuntimePluginPin, RuntimePluginScope

from ._runtime_plugin_authority import admit_plugin_authority, current_plugin_credential, fence_plugin_authority
from ._runtime_plugins import _authorize

if TYPE_CHECKING:
    from engine.models import Range, RuntimePluginInstallation, RuntimePluginPackBinding


@dataclass(frozen=True)
class RuntimePluginPackView:
    """Current tenant pack selection and the installation readiness it depends on."""

    id: UUID
    organization_uuid: UUID
    pack_id: str
    pack_digest: str
    installation_id: UUID
    bindings: dict[str, Any]
    enabled: bool
    installation_state: str


@dataclass(frozen=True, slots=True)
class RuntimePluginBindingAdmission:
    """Invocation-local authority for an exact pack binding committed under CMS locks."""

    credential: CredentialContext
    scope: RuntimePluginScope
    installation_id: UUID
    bindings: dict[str, Any]
    enabled: bool
    authority: dict[str, object]


@dataclass(frozen=True, slots=True)
class RuntimePluginBindingRemovalAdmission:
    """Exact content-authorized binding cleanup; no executable-installation grant."""

    credential: CredentialContext
    scope: RuntimePluginScope
    resource_scope: ResourceScope
    binding_uuid: UUID | None
    binding_digest: str | None


def prepare_runtime_plugin_binding(
    credential: CredentialContext,
    scope: RuntimePluginScope,
    installation_id: UUID,
    bindings: object,
    *,
    enabled: bool = True,
) -> RuntimePluginBindingAdmission:
    """Complete native admission before the source/installation transaction opens."""
    credential = current_plugin_credential(credential)
    _authorize(credential, scope.organization_uuid)
    authority = admit_plugin_authority(credential, scope.organization_uuid)
    try:
        parsed = PluginTargetBindings.model_validate(bindings)
        if type(enabled) is not bool:
            raise ValueError("Invalid binding state")
    except ValueError:
        raise ValidationError("The pack digest or plugin bindings are invalid") from None
    return RuntimePluginBindingAdmission(
        credential, scope, installation_id, parsed.model_dump(mode="json"), enabled, authority
    )


def prepare_runtime_plugin_pack_binding_removal(
    credential: CredentialContext, scope: RuntimePluginScope
) -> RuntimePluginBindingRemovalAdmission:
    """Authorize the exact pack cleanup using its organization's content action."""
    from engine.models import RuntimePluginPackBinding
    from shared.authorization import AuthorizationRequest
    from shared.authorization.policy import require_authorization

    credential = current_plugin_credential(credential)
    target = TargetRef("organization", scope.organization_uuid)
    resource_scope = resolve_credential_scope(target)
    require_authorization(
        AuthorizationRequest(
            credential.principal, "organization.manage_content", target, resource_scope, credential.ceiling
        )
    )
    row = (
        RuntimePluginPackBinding.objects.filter(organization_uuid=scope.organization_uuid, pack_id=scope.pack_id)
        .values("pk", "pack_digest")
        .first()
    )
    return RuntimePluginBindingRemovalAdmission(
        credential, scope, resource_scope, row["pk"] if row else None, row["pack_digest"] if row else None
    )


def bind_runtime_plugin(
    user: CredentialContext | RuntimePluginBindingAdmission,
    scope: RuntimePluginScope,
    installation_id: UUID,
    bindings: object,
    *,
    enabled: bool = True,
    audit: RequestAudit | None = None,
) -> RuntimePluginPackView:
    """Authorize and update a pack's selection, without changing any range pin."""
    from engine.models import RuntimePluginInstallation, RuntimePluginPackBinding

    admission = (
        user
        if isinstance(user, RuntimePluginBindingAdmission)
        else prepare_runtime_plugin_binding(user, scope, installation_id, bindings, enabled=enabled)
    )
    if (admission.scope, admission.installation_id, admission.bindings, admission.enabled) != (
        scope,
        installation_id,
        PluginTargetBindings.model_validate(bindings).model_dump(mode="json"),
        enabled,
    ):
        raise ValidationError("Plugin binding admission changed")
    user, authority = admission.credential, admission.authority
    organization_uuid = scope.organization_uuid
    pack_id, pack_digest = scope.pack_id, scope.pack_digest
    try:
        RuntimePluginScope(organization_uuid=organization_uuid, pack_id=pack_id, pack_digest=pack_digest)
        parsed = PluginTargetBindings.model_validate(bindings)
    except ValueError:
        raise ValidationError("The pack digest or plugin bindings are invalid") from None
    if not isinstance(enabled, bool):
        raise ValidationError("The binding state is invalid")
    with transaction.atomic():
        installation = (
            RuntimePluginInstallation.objects.select_for_update()
            .filter(
                pk=installation_id,
                organization_uuid=organization_uuid,
            )
            .first()
        )
        if installation is None:
            raise ValidationError("The plugin is unavailable")
        _validate_binding_authority(user, installation, authority)
        if enabled and installation.state != "ready":
            raise ValidationError("The plugin must pass compatibility checks before binding")
        _validate_pack_pin(installation, parsed, organization_uuid, pack_id, pack_digest)
        row, _ = RuntimePluginPackBinding.objects.update_or_create(
            organization_uuid=organization_uuid,
            pack_id=pack_id,
            defaults={
                "installation": installation,
                "pack_digest": pack_digest,
                "bindings": parsed.model_dump(mode="json"),
                "enabled": enabled,
            },
        )
        attribution = audit or RequestAudit()
        actor: dict[str, Any] = dict(principal_actor_fields(user.principal))
        audit_log(
            AuditEvent(
                entity_type=AuditEntityType.RUNTIME_PLUGIN,
                entity_id=0,
                entity_ref=str(row.id),
                action=AuditAction.UPDATE,
                **actor,
                request_id=attribution.request_id,
                source_ip=attribution.source_ip,
                user_agent=attribution.user_agent,
                new_state={
                    "organization_uuid": str(organization_uuid),
                    "pack_digest": pack_digest,
                    "installation_id": str(installation.id),
                    "enabled": enabled,
                },
            ),
            strict=True,
        )
        return _view(row)


def list_runtime_plugin_bindings(user: CredentialContext, organization_uuid: UUID) -> list[RuntimePluginPackView]:
    """List explicit selections owned by the authorized organization."""
    from engine.models import RuntimePluginPackBinding

    organization_uuid = _authorize(user, organization_uuid)
    return [
        _view(row)
        for row in RuntimePluginPackBinding.objects.filter(
            organization_uuid=organization_uuid,
        )
        .select_related("installation")
        .order_by("pack_digest")
    ]


def remove_runtime_plugin_pack_binding(
    user: RuntimePluginBindingRemovalAdmission,
    organization_uuid: UUID,
    pack_id: str,
    *,
    audit: RequestAudit | None = None,
) -> bool:
    """Delete a pack's current selection; range pins and installations are retained.

    Locks the installation before the binding, the order every binding writer
    and launch admission uses. Returns whether a selection existed.
    """
    from engine.models import RuntimePluginInstallation, RuntimePluginPackBinding

    if not isinstance(user, RuntimePluginBindingRemovalAdmission) or (
        user.scope.organization_uuid,
        user.scope.pack_id,
    ) != (organization_uuid, pack_id):
        raise ValidationError("Plugin binding cleanup admission changed")
    with transaction.atomic():
        candidate = (
            RuntimePluginPackBinding.objects.filter(organization_uuid=organization_uuid, pack_id=pack_id)
            .values_list("pk", "installation_id")
            .first()
        )
        if candidate is None:
            _fence_binding_removal(user)
            if user.binding_uuid is not None:
                raise ValidationError("Plugin binding cleanup changed")
            return False
        if candidate[0] != user.binding_uuid:
            raise ValidationError("Plugin binding cleanup changed")
        RuntimePluginInstallation.objects.select_for_update().get(pk=candidate[1])
        row = RuntimePluginPackBinding.objects.select_for_update().get(pk=candidate[0])
        _fence_binding_removal(user)
        if row.pack_digest != user.binding_digest:
            raise ValidationError("Plugin binding cleanup revision changed")
        previous = {
            "organization_uuid": str(organization_uuid),
            "pack_digest": row.pack_digest,
            "installation_id": str(row.installation_id),
            "enabled": row.enabled,
        }
        reference = str(row.id)
        row.delete()
        attribution = audit or RequestAudit()
        audit_log(
            AuditEvent(
                entity_type=AuditEntityType.RUNTIME_PLUGIN,
                entity_id=0,
                entity_ref=reference,
                action=AuditAction.DELETE,
                **principal_actor_fields(user.credential.principal),
                request_id=attribution.request_id,
                source_ip=attribution.source_ip,
                user_agent=attribution.user_agent,
                previous_state=previous,
            ),
            strict=True,
        )
        return True


def _fence_binding_removal(admission: RuntimePluginBindingRemovalAdmission) -> None:
    """Consume only the exact content admission while binding rows are locked."""
    try:
        if (
            resolve_local_credential(admission.credential) != admission.credential
            or resolve_credential_scope(TargetRef("organization", admission.scope.organization_uuid))
            != admission.resource_scope
        ):
            raise ValueError("Plugin cleanup authority changed")
    except ValueError:
        raise ValidationError("Plugin binding cleanup authority changed") from None


def has_runtime_plugin_binding(organization_uuid: UUID, pack_id: str) -> bool:
    """Whether a trusted launch scope needs cold plugin admission, even if disabled."""
    from engine.models import RuntimePluginPackBinding

    return RuntimePluginPackBinding.objects.filter(
        organization_uuid=organization_uuid,
        pack_id=pack_id,
    ).exists()


def resolve_runtime_plugin_pin(
    scope: RuntimePluginScope, plan: dict[str, Any], *, backend: str | None = None
) -> RuntimePluginPin | None:
    """Resolve under the installation lock held through range creation.

    Called only with the CMS launch boundary's authorized organization, verified
    pack digest and compiled plan, inside the Engine range transaction.
    """
    from engine.models import RuntimePluginInstallation, RuntimePluginPackBinding

    organization_uuid, pack_digest = scope.organization_uuid, scope.pack_digest

    candidate = (
        RuntimePluginPackBinding.objects.filter(
            organization_uuid=organization_uuid,
            pack_id=scope.pack_id,
        )
        .values_list("pk", "installation_id")
        .first()
    )
    if candidate is None:
        return None
    # All writers lock installation before binding. Recheck after acquiring both
    # locks: a concurrent version switch must not admit a partially old pin.
    installation = RuntimePluginInstallation.objects.select_for_update().get(pk=candidate[1])
    binding = RuntimePluginPackBinding.objects.select_for_update().get(pk=candidate[0])
    if binding.installation_id != installation.id:
        raise ValidationError("The pack's runtime plugin changed during launch; retry the launch")
    if binding.pack_digest != pack_digest:
        raise ValidationError("The pack changed; an administrator must bind its new version before launch")
    if not binding.enabled or installation.state != "ready" or installation.organization_uuid != organization_uuid:
        raise ValidationError("The pack's runtime plugin is not enabled and ready")
    try:
        manifest = PluginManifest.model_validate(installation.manifest)
        if manifest.digest != installation.manifest_digest:
            raise ValueError("Stored plugin identity mismatch")
        pin = RuntimePluginPin(
            installation_id=installation.id,
            organization_uuid=organization_uuid,
            pack_id=scope.pack_id,
            pack_digest=pack_digest,
            manifest=manifest,
            bindings=binding.bindings,
        )
        pin.bindings.validate_plan(plan)
        if backend is not None:
            pin.bindings.validate_provider(backend)
        return pin
    except ValueError:
        raise ValidationError("The installed plugin does not match this pack's compiled guests") from None


def persist_runtime_plugin_pin(target: Range, pin: RuntimePluginPin | None) -> None:
    """Retain the admitted installation and guest mapping for the range lifetime."""
    from engine.models import RuntimePluginRangeBinding

    if pin is not None:
        RuntimePluginRangeBinding.objects.create(
            range=target,
            installation_id=pin.installation_id,
            pin=pin.model_dump(mode="json"),
            pin_digest=pin.digest,
        )


def retained_runtime_plugin_pin(target: Range) -> RuntimePluginPin | None:
    """Read the original pin for retries/cleanup even after registry retirement."""
    from engine.models import RuntimePluginRangeBinding

    row = RuntimePluginRangeBinding.objects.filter(range=target).first()
    if row is None:
        return None
    try:
        pin = RuntimePluginPin.model_validate(row.pin)
        # Check the exact retained bytes, not a re-serialized model: adding a
        # defaulted binding field must not make pre-upgrade pins undeletable.
        if canonical_digest(row.pin) != row.pin_digest or pin.installation_id != row.installation_id:
            raise ValueError("Stored pin identity mismatch")
        pin.bindings.validate_plan(target.range_config)
        if target.range_backend:
            pin.bindings.validate_provider(str(target.range_backend))
        return pin
    except ValueError:
        raise ValidationError("The range's runtime plugin binding is invalid") from None


def _view(row: RuntimePluginPackBinding) -> RuntimePluginPackView:
    """Project a stored pack binding and current installation state."""
    bindings = PluginTargetBindings.model_validate(row.bindings).model_dump(mode="json")
    return RuntimePluginPackView(
        row.id,
        row.organization_uuid,
        row.pack_id,
        row.pack_digest,
        row.installation_id,
        bindings,
        row.enabled,
        row.installation.state,
    )


def _validate_binding_authority(
    user: CredentialContext,
    installation: RuntimePluginInstallation,
    authority: dict[str, object],
) -> None:
    """Fence the admitted plugin installation and local credential while its binding is locked."""
    from ._runtime_plugin_authority import validate_plugin_scope

    validate_plugin_scope(installation, authority)
    fence_plugin_authority(user, authority)


def _validate_pack_pin(
    installation: RuntimePluginInstallation,
    parsed: PluginTargetBindings,
    organization_uuid: UUID,
    pack_id: str,
    pack_digest: str,
) -> None:
    """Validate the stored immutable manifest and exact pack bindings before publishing a selection."""
    try:
        manifest = PluginManifest.model_validate(installation.manifest)
        if manifest.digest != installation.manifest_digest:
            raise ValueError("Stored plugin identity mismatch")
        parsed.validate_manifest(manifest)
        RuntimePluginPin(
            installation_id=installation.id,
            organization_uuid=organization_uuid,
            pack_id=pack_id,
            pack_digest=pack_digest,
            manifest=manifest,
            bindings=parsed,
        )
    except ValueError:
        raise ValidationError("The bindings do not match the installed plugin declaration") from None
