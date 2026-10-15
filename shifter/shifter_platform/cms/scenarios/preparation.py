"""Select authored preparation intent from an accessible, registered private pack."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

from pydantic import ValidationError as ModelValidationError

from cms.exceptions import CMSError
from cms.scenarios.realizability import _trusted_scenario_path
from cms.scenarios.registry import check_scenario_access
from engine.services import request_artifact_preparation
from shared.credentials import CredentialContext
from shared.exceptions import ValidationError
from shared.raes.preparation_contract import (
    PreparationPackage,
    load_preparation_input_bindings,
    load_preparation_requirement,
)

if TYPE_CHECKING:
    from django.contrib.auth.models import User

    from engine.services import PreparationView
    from shared.audit import RequestAudit


def prepare_registered_artifact(
    user: User | CredentialContext,
    scenario_id: str,
    requirement_address: str,
    adapter_id: UUID,
    specification_id: str,
    *,
    audit: RequestAudit | None = None,
) -> PreparationView:
    """Resolve private bytes server-side; HTTP input cannot invent build permission.

    Catalog access and package conformance are independent of artifact supply.
    The existing trusted staging context checks immutable bytes and contains
    private object packs until public RAES has extracted the exact requirement.
    """
    from cms.models import RaesPackageSource

    if not isinstance(user, CredentialContext) and not (
        user and user.is_active and user.has_perm("engine.prepare_artifacts")
    ):
        raise ValidationError("Artifact preparation access is not permitted")
    source = RaesPackageSource.objects.filter(scenario_id=scenario_id).first()
    scope = None
    try:
        if isinstance(user, CredentialContext):
            from cms.services._content_authority import content_scope, require_content_scope

            scope = content_scope(source)
            require_content_scope(user, scope, "prepare_content")
        detail = check_scenario_access(scenario_id, user, scope=scope)
    except (ValueError, CMSError) as exc:
        raise ValidationError("The preparation package is unavailable") from exc
    if source is None or not detail.get("launchable") or not source.package_digest:
        raise ValidationError("The preparation package is not trusted for use")
    with _trusted_scenario_path(source) as (scenario_path, _):
        if scenario_path is None:
            raise ValidationError("The preparation package integrity check failed")
        try:
            requirement = load_preparation_requirement(scenario_path, requirement_address)
            package = PreparationPackage(
                scenario_id=source.scenario_id,
                package_digest=source.package_digest,
                lock_digest=source.lock_digest,
                requirement_address=requirement_address,
                requirement=requirement,
                specification_id=specification_id,
                input_bindings=load_preparation_input_bindings(scenario_path, requirement),
            )
        except (ValueError, ModelValidationError) as exc:
            raise ValidationError("The authored preparation requirement is unavailable") from exc
    if isinstance(user, CredentialContext):
        # Staging is external I/O. Recheck the original reference and live
        # authority before admitting a durable job from those bytes.
        refreshed = RaesPackageSource.objects.filter(pk=source.pk).first()
        fields = (
            "scenario_id",
            "source_kind",
            "contract_kind",
            "contract_profile",
            "package_version",
            "conformance_status",
            "conformance_report_ref",
            "scope_generation",
            "package_ref",
            "package_digest",
            "lock_ref",
            "lock_digest",
            "scope_kind",
            "account_uuid",
            "organization_uuid",
            "workspace_uuid",
            "owner_principal_uuid",
        )
        if refreshed is None or any(getattr(refreshed, field) != getattr(source, field) for field in fields):
            raise ValidationError("The preparation package is unavailable")
        try:
            require_content_scope(user, scope, "prepare_content")
        except CMSError:
            raise ValidationError("The preparation package is unavailable") from None
    return request_artifact_preparation(user, adapter_id, package.model_dump(mode="json"), scope=scope, audit=audit)
