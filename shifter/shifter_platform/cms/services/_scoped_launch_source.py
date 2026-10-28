"""Source eligibility and current authority shared by native launch paths."""

from __future__ import annotations

from cms.exceptions import CMSError
from cms.models import RaesPackageSource
from shared.authorization import TargetRef
from shared.credentials import CredentialContext
from shared.identity_scope import ResourceScope
from shared.schemas.cms_projections import ScenarioProjection
from workspaces.services import resolve_range_scope_binding

from ._content_authority import require_content_scope

_SOURCE_UNAVAILABLE = "Range source unavailable"


_SOURCE_FIELDS = (
    "scenario_id",
    "contract_kind",
    "contract_profile",
    "source_kind",
    "package_ref",
    "package_name",
    "package_digest",
    "lock_digest",
    "lock_ref",
    "package_version",
    "scope_kind",
    "account_uuid",
    "organization_uuid",
    "workspace_uuid",
    "owner_principal_uuid",
    "scope_generation",
    "is_operator_published",
    "conformance_status",
    "conformance_report_ref",
    "provenance",
)


def source_launch_binding(source: RaesPackageSource) -> dict[str, object]:
    """Immutable registered content/custody facts inspected before any staging."""
    return {name: getattr(source, name) for name in _SOURCE_FIELDS}


def source_launch_metadata(scenario_id: str) -> tuple[bool, bool]:
    """Pin the effective availability overlay, including an absent default row."""
    from cms.models import ScenarioMetadata

    metadata = ScenarioMetadata.objects.filter(scenario_id=scenario_id).values_list("enabled", "staff_only").first()
    return metadata if metadata is not None else (True, False)


def revalidate_scoped_launch(
    credential: CredentialContext,
    scope: ResourceScope,
    source: RaesPackageSource,
    *,
    authority_action: str | None = None,
    authority_target: TargetRef | None = None,
) -> CredentialContext:
    """Repeat original authority and source eligibility before irreversible effects."""
    from cms.scenarios.registry import check_scenario_access

    if (authority_action is None) != (authority_target is None):
        raise CMSError("Range authority unavailable")
    if authority_action is None:
        current = require_content_scope(credential, scope, "launch_range")
        entry = check_scenario_access(source.scenario_id, current, scope=scope)
    else:
        from shared.authorization import AuthorizationRequest
        from shared.authorization.policy import require_authorization
        from shared.credentials import resolve_current_credential
        from workspaces.services import resolve_resource_scope

        if authority_target is None:
            raise CMSError("Range authority unavailable")
        current = resolve_current_credential(credential)
        resolve_resource_scope(scope)
        require_authorization(
            AuthorizationRequest(current.principal, authority_action, authority_target, scope, current.ceiling)
        )
        # The CTF owner authorized this immutable source when configuring the
        # event. A range operator may be delegated only ``event.manage_ranges``
        # and must not need an unrelated account content grant at dispatch.
        # Revalidate the source's current custody and publication facts here;
        # the owning CTF service supplies the event target and exact scope.
        entry = _event_source_entry(source, scope)
    if not entry.get("launchable") or not entry.get("enabled", True):
        raise CMSError(_SOURCE_UNAVAILABLE)
    if not RaesPackageSource.objects.filter(pk=source.pk, **source_launch_binding(source)).exists():
        raise CMSError("Range source changed")
    if scope.kind == "installation":
        if current.principal.kind != "service":
            raise CMSError(_SOURCE_UNAVAILABLE)
        resolve_range_scope_binding(scope)
    elif authority_target is None:
        resolve_range_scope_binding(scope, member=current.principal)
    else:
        resolve_range_scope_binding(scope)
    return current


def _event_source_entry(source: RaesPackageSource, scope: ResourceScope) -> ScenarioProjection:
    """Recheck exact event-configured source custody without unrelated content grants."""
    from ._content_authority import content_scope

    source_scope = content_scope(source)
    shared = source_scope.kind == "installation" and source.is_operator_published
    if not shared and source_scope != scope:
        raise CMSError(_SOURCE_UNAVAILABLE)
    from cms.models import ScenarioMetadata
    from cms.scenarios.registry import _raes_launchable, _raes_source_to_dict

    metadata_row = ScenarioMetadata.objects.filter(scenario_id=source.scenario_id).first()
    metadata = (
        None if metadata_row is None else {"enabled": metadata_row.enabled, "staff_only": metadata_row.staff_only}
    )
    return _raes_source_to_dict(source, metadata=metadata, launchable=_raes_launchable(source))
