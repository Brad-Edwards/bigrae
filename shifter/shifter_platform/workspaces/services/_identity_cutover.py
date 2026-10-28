"""Issue-specific SQL conversion for the coordinated S8 identity cutover."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict
from uuid import NAMESPACE_URL, UUID, uuid5

from django.db import transaction
from django.utils import timezone

from workspaces.identity_mapping import legacy_membership_role_blocker, plan_legacy_account_mapping
from workspaces.models import (
    Account,
    AuthorizationGroup,
    AuthorizationOperation,
    AuthorizationPolicy,
    Organization,
    OrganizationMembership,
    Workspace,
    WorkspaceMembership,
    WorkspaceOwnership,
)

from ._identity_cutover_state import (
    IdentityCutoverConflict,
    IdentityCutoverStateView,
    _state_view,
    activate_identity_cutover,
    begin_identity_cutover,
    bind_identity_cutover_provider,
    get_identity_cutover_state,
    record_identity_cutover_bootstrap,
    record_identity_cutover_policy_intent,
)

__all__ = [
    "IdentityCutoverConflict",
    "IdentityCutoverStateView",
    "_state_view",
    "activate_identity_cutover",
    "apply_identity_cutover_sql",
    "begin_identity_cutover",
    "bind_identity_cutover_provider",
    "get_identity_cutover_state",
    "identity_cutover_disposition",
    "identity_cutover_relationships",
    "identity_cutover_resource_mappings",
    "plan_identity_cutover_accounts",
    "record_identity_cutover_bootstrap",
    "record_identity_cutover_policy_intent",
    "verify_identity_cutover_hierarchy",
]

_CUTOVER_PLAN_SHAPE_CHANGED = "cutover plan shape changed"
_CUTOVER_RESOURCE_PLAN_IS_INVALID = "cutover resource plan is invalid"
_LEGACY_MEMBERSHIP_POLICY_DISPOSITION_IS_REQUIRED = "legacy membership policy disposition is required"


def _plan_int(item: Mapping[str, object], key: str) -> int:
    """Read a persisted scalar identifier without coercing arbitrary objects."""
    value = item.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise IdentityCutoverConflict(_CUTOVER_PLAN_SHAPE_CHANGED)
    try:
        return int(value)
    except ValueError as exc:
        raise IdentityCutoverConflict(_CUTOVER_PLAN_SHAPE_CHANGED) from exc


def _plan_uuid(item: Mapping[str, object], key: str) -> UUID:
    """Decode an exact UUID from the retained cutover plan."""
    value = item.get(key)
    if not isinstance(value, (UUID, str)):
        raise IdentityCutoverConflict(_CUTOVER_PLAN_SHAPE_CHANGED)
    try:
        return UUID(str(value))
    except ValueError as exc:
        raise IdentityCutoverConflict(_CUTOVER_PLAN_SHAPE_CHANGED) from exc


def plan_identity_cutover_accounts(
    *,
    principal_by_user: Mapping[int, UUID],
    owner_ids_by_workspace: Mapping[int, set[int]],
    account_types_by_organization: Mapping[UUID, str],
) -> dict[str, object]:
    """Project the legacy hierarchy through the tenancy-owned classifier."""
    plan = plan_legacy_account_mapping(
        principal_by_user=principal_by_user,
        owner_ids_by_workspace=owner_ids_by_workspace,
        account_types_by_organization=account_types_by_organization,
    )
    return {
        "individuals": [asdict(item) for item in plan.individuals],
        "organizations": [asdict(item) for item in plan.organizations],
        "memberships": [asdict(item) for item in plan.memberships],
        "blockers": [asdict(item) for item in plan.blockers],
    }


def identity_cutover_disposition() -> tuple[dict[str, object], list[dict[str, object]]]:
    """Classify retained tenancy and pending authorization state."""
    pending = AuthorizationOperation.objects.filter(
        state__in=(AuthorizationOperation.State.REQUESTED, AuthorizationOperation.State.UNRESOLVED)
    ).count()
    blockers = [{"reason": "authorization_operation_unresolved", "entity_count": pending}] if pending else []
    return (
        {
            "accounts": {"action": "map_without_reassignment"},
            "groups_and_roles": {
                "groups": AuthorizationGroup.objects.count(),
                "policies": AuthorizationPolicy.objects.count(),
                "action": "retain_sql_metadata_and_native_relationships",
            },
            "operations": {
                "count": AuthorizationOperation.objects.count(),
                "action": "retain_terminal_and_reconcile_pending",
            },
        },
        blockers,
    )


def identity_cutover_resource_mappings(plan: Mapping[str, object]) -> dict[int, tuple[int, int | None, int | None]]:
    """Resolve the applied plan to scalar mappings for resource-owning domains."""
    mappings: dict[int, tuple[int, int | None, int | None]] = {}
    individuals = plan.get("individuals")
    organizations = plan.get("organizations")
    if not isinstance(individuals, Sequence) or not isinstance(organizations, Sequence):
        raise IdentityCutoverConflict(_CUTOVER_RESOURCE_PLAN_IS_INVALID)
    retired_organizations: set[int | None] = set()
    for item in individuals:
        if not isinstance(item, Mapping):
            raise IdentityCutoverConflict(_CUTOVER_RESOURCE_PLAN_IS_INVALID)
        retired_organizations.add(_plan_int(item, "legacy_organization_id"))
        account = Account.objects.get(individual_principal_uuid=_plan_uuid(item, "principal_uuid"))
        mappings[_plan_int(item, "legacy_workspace_id")] = (account.pk, None, None)
    for item in organizations:
        if not isinstance(item, Mapping):
            raise IdentityCutoverConflict(_CUTOVER_RESOURCE_PLAN_IS_INVALID)
        Organization.objects.get(pk=_plan_int(item, "organization_id"), account__isnull=False)
    _verify_retained_hierarchy(retired_organizations)
    # Existing account bindings are just as authoritative as bindings applied
    # by this plan. Include every retained workspace without inventing a parent.
    for workspace_id, account_id, organization_id in Workspace.objects.filter(
        organization__account__isnull=False
    ).values_list("pk", "organization__account_id", "organization_id"):
        mappings[workspace_id] = (account_id, organization_id, workspace_id)
    return mappings


def identity_cutover_relationships(plan: Mapping[str, object]) -> set[tuple[str, str, str]]:
    """Project retained account ancestry and planned administrative grants."""
    relationships: set[tuple[str, str, str]] = set()
    for account_uuid in Account.objects.order_by("uuid").values_list("uuid", flat=True):
        relationships.add(("installation:root", "installation", f"account:{account_uuid}"))
    for organization in Organization.objects.filter(account__isnull=False).select_related("account").order_by("uuid"):
        account = organization.account
        if account is None:
            raise IdentityCutoverConflict("retained organization account is unavailable")
        relationships.add((f"account:{account.uuid}", "account", f"organization:{organization.uuid}"))
    for workspace in Workspace.objects.filter(organization__account__isnull=False).select_related("organization"):
        relationships.add(
            (f"organization:{workspace.organization.uuid}", "organization", f"workspace:{workspace.uuid}")
        )
    memberships = plan.get("memberships")
    if not isinstance(memberships, Sequence):
        raise IdentityCutoverConflict("cutover relationship plan is invalid")
    for item in memberships:
        if not isinstance(item, Mapping):
            raise IdentityCutoverConflict("cutover relationship plan is invalid")
        relationship = _membership_administrator_relationship(item)
        if relationship is not None:
            relationships.add(relationship)
    return relationships


def _individual_account_uuid(principal_uuid: UUID) -> UUID:
    """Derive the stable individual account identity from its principal."""
    return uuid5(NAMESPACE_URL, f"shifter:individual-account:{principal_uuid}")


def _shared_account_uuid(organization_uuid: UUID) -> UUID:
    """Derive the stable shared account identity from its legacy organization."""
    return uuid5(NAMESPACE_URL, f"shifter:shared-account:{organization_uuid}")


def apply_identity_cutover_sql(plan: Mapping[str, object]) -> None:
    """Apply the planned workspaces facts under one bounded SQL transaction."""
    individuals = plan.get("individuals")
    organizations = plan.get("organizations")
    memberships = plan.get("memberships")
    if (
        not isinstance(individuals, Sequence)
        or not isinstance(organizations, Sequence)
        or not isinstance(memberships, Sequence)
    ):
        raise IdentityCutoverConflict(_CUTOVER_PLAN_SHAPE_CHANGED)
    with transaction.atomic():
        for item in individuals:
            if not isinstance(item, Mapping):
                raise IdentityCutoverConflict(_CUTOVER_PLAN_SHAPE_CHANGED)
            _apply_individual(item)
        for item in organizations:
            if not isinstance(item, Mapping):
                raise IdentityCutoverConflict(_CUTOVER_PLAN_SHAPE_CHANGED)
            _apply_shared_organization(item)
        for item in memberships:
            if not isinstance(item, Mapping):
                raise IdentityCutoverConflict(_CUTOVER_PLAN_SHAPE_CHANGED)
            _apply_membership(item)


def verify_identity_cutover_hierarchy(plan: Mapping[str, object]) -> None:
    """Verify the applied account/default hierarchy before admission activates."""
    individuals = plan.get("individuals")
    organizations = plan.get("organizations")
    if not isinstance(individuals, Sequence) or not isinstance(organizations, Sequence):
        raise IdentityCutoverConflict(_CUTOVER_PLAN_SHAPE_CHANGED)
    retired_organizations: set[int | None] = set()
    for item in individuals:
        if not isinstance(item, Mapping):
            raise IdentityCutoverConflict(_CUTOVER_PLAN_SHAPE_CHANGED)
        retired_organizations.add(_plan_int(item, "legacy_organization_id"))
        _verify_individual_hierarchy(item)
    for item in organizations:
        if not isinstance(item, Mapping):
            raise IdentityCutoverConflict(_CUTOVER_PLAN_SHAPE_CHANGED)
        _verify_shared_hierarchy(item)
    _verify_retained_hierarchy(retired_organizations)
    _verify_membership_dispositions(plan)


def _verify_membership_dispositions(plan: Mapping[str, object]) -> None:
    """No retained user-keyed role or omitted membership may survive activation."""
    for model in (OrganizationMembership, WorkspaceMembership):
        if model.objects.filter(user_id__isnull=False).exists() or model.objects.exclude(role="").exists():
            raise IdentityCutoverConflict("legacy membership disposition is incomplete")
    memberships = plan.get("memberships", [])
    if not isinstance(memberships, Sequence):
        raise IdentityCutoverConflict("cutover membership plan shape changed")
    for item in memberships:
        if not isinstance(item, Mapping):
            raise IdentityCutoverConflict("cutover membership plan shape changed")
        _verify_planned_membership(item)


def _verify_retained_hierarchy(retired_organizations: set[int | None]) -> None:
    """Check all retained facts, including those already on the prepared schema."""
    for account in Account.objects.iterator():
        children = Organization.objects.filter(account=account)
        if account.kind == Account.Kind.INDIVIDUAL:
            if children.exists():
                raise IdentityCutoverConflict("individual account hierarchy has unexpected subdivisions")
        elif children.filter(is_default=True).count() != 1:
            raise IdentityCutoverConflict("shared account default organization is incomplete")
    for organization in Organization.objects.exclude(pk__in=retired_organizations).select_related("account").iterator():
        if organization.account is None or organization.account.kind == Account.Kind.INDIVIDUAL:
            raise IdentityCutoverConflict("retained organization account is unavailable")
        defaults = Workspace.objects.filter(organization=organization, is_default=True)
        if (
            defaults.count() != 1
            or not defaults.filter(archived_at__isnull=True, personal_for_user__isnull=True).exists()
        ):
            raise IdentityCutoverConflict("shared organization default workspace is incomplete")


def _apply_individual(item: Mapping[str, object]) -> None:
    """Archive one synthetic personal subdivision and preserve its durable facts."""
    try:
        principal_uuid = _plan_uuid(item, "principal_uuid")
        user_id = _plan_int(item, "user_id")
        organization_id = _plan_int(item, "legacy_organization_id")
        workspace_id = _plan_int(item, "legacy_workspace_id")
    except (KeyError, TypeError, ValueError) as exc:
        raise IdentityCutoverConflict(_CUTOVER_PLAN_SHAPE_CHANGED) from exc
    organization = Organization.objects.select_for_update().filter(pk=organization_id).first()
    workspace = Workspace.objects.select_for_update().filter(pk=workspace_id, organization_id=organization_id).first()
    existing_account = Account.objects.filter(individual_principal_uuid=principal_uuid).first()
    already_applied = _individual_already_applied(existing_account, workspace, principal_uuid)
    if already_applied:
        return
    if (
        organization is None
        or organization.account_id is not None
        or workspace is None
        or workspace.personal_for_user_id != user_id
    ):
        raise IdentityCutoverConflict("planned personal hierarchy changed")
    Account.objects.get_or_create(
        individual_principal_uuid=principal_uuid,
        defaults={
            "uuid": _individual_account_uuid(principal_uuid),
            "kind": Account.Kind.INDIVIDUAL,
            "name": "Individual account",
        },
    )
    OrganizationMembership.objects.filter(organization=organization, user_id=user_id).update(
        user_id=None,
        principal_uuid=principal_uuid,
        role="",
    )
    WorkspaceMembership.objects.filter(workspace=workspace, user_id=user_id).update(
        user_id=None,
        principal_uuid=principal_uuid,
        role="",
    )
    WorkspaceOwnership.objects.get_or_create(workspace=workspace, principal_uuid=principal_uuid)
    workspace.personal_for_user_id = None
    workspace.archived_at = workspace.archived_at or timezone.now()
    workspace.save(update_fields=["personal_for_user", "archived_at", "updated_at"])


def _apply_shared_organization(item: Mapping[str, object]) -> None:
    """Bind one retained organization and its planned default workspace."""
    try:
        organization_id = _plan_int(item, "organization_id")
        workspace_id = _plan_int(item, "default_workspace_id")
        account_kind = str(item["account_kind"])
    except (KeyError, TypeError, ValueError) as exc:
        raise IdentityCutoverConflict(_CUTOVER_PLAN_SHAPE_CHANGED) from exc
    organization = Organization.objects.select_for_update().filter(pk=organization_id).first()
    workspace = Workspace.objects.select_for_update().filter(pk=workspace_id, organization_id=organization_id).first()
    if organization is None or workspace is None or account_kind not in {Account.Kind.TEAM, Account.Kind.ENTERPRISE}:
        raise IdentityCutoverConflict("planned shared hierarchy changed")
    account, created = Account.objects.get_or_create(
        uuid=_shared_account_uuid(organization.uuid),
        defaults={"kind": account_kind, "name": organization.name},
    )
    if not created and account.kind != account_kind:
        raise IdentityCutoverConflict("planned account kind changed")
    if organization.account_id not in {None, account.pk}:
        raise IdentityCutoverConflict("planned shared hierarchy changed")
    organization.account = account
    organization.is_default = True
    organization.save(update_fields=["account", "is_default", "updated_at"])
    workspace.is_default = True
    workspace.save(update_fields=["is_default", "updated_at"])


def _apply_membership(item: Mapping[str, object]) -> None:
    """Replace one legacy user/role row with its principal membership and owner fact."""
    try:
        scope_kind = str(item["scope_kind"])
        scope_id = _plan_int(item, "scope_id")
        user_id = _plan_int(item, "user_id")
        principal_uuid = _plan_uuid(item, "principal_uuid")
        role = str(item["role"])
    except (KeyError, TypeError, ValueError) as exc:
        raise IdentityCutoverConflict(_CUTOVER_PLAN_SHAPE_CHANGED) from exc
    if legacy_membership_role_blocker(scope_kind, role) is not None:
        raise IdentityCutoverConflict(_LEGACY_MEMBERSHIP_POLICY_DISPOSITION_IS_REQUIRED)
    model, parent_field = _membership_target(scope_kind, scope_id, principal_uuid, role)
    rows = model.objects.select_for_update().filter(**{parent_field: scope_id})
    legacy = rows.filter(user_id=user_id, principal_uuid__isnull=True).first()
    principal = rows.filter(user__isnull=True, principal_uuid=principal_uuid).first()
    _verify_membership_rows(legacy, principal, role)
    if legacy is not None and principal is not None:
        legacy.delete()
    elif legacy is not None:
        legacy.user_id = None
        legacy.principal_uuid = principal_uuid
        legacy.role = ""
        legacy.save(update_fields=["user", "principal_uuid", "role", "updated_at"])
    if rows.filter(user__isnull=True, principal_uuid=principal_uuid).count() != 1:
        raise IdentityCutoverConflict("planned membership changed")


def _verify_individual_hierarchy(item: Mapping[str, object]) -> None:
    """Require one individual account without retained organizational subdivisions."""
    account = Account.objects.filter(individual_principal_uuid=_plan_uuid(item, "principal_uuid")).first()
    if account is None or account.kind != Account.Kind.INDIVIDUAL:
        raise IdentityCutoverConflict("individual account hierarchy is incomplete")
    if Organization.objects.filter(account=account).exists():
        raise IdentityCutoverConflict("individual account hierarchy has unexpected subdivisions")


def _verify_shared_hierarchy(item: Mapping[str, object]) -> None:
    """Require exactly the planned default organization and workspace after conversion."""
    organization = Organization.objects.filter(pk=_plan_int(item, "organization_id"), account__isnull=False).first()
    if organization is None or organization.account_id is None or not organization.is_default:
        raise IdentityCutoverConflict("shared account default organization is incomplete")
    if Organization.objects.filter(account_id=organization.account_id, is_default=True).count() != 1:
        raise IdentityCutoverConflict("shared account default organization is ambiguous")
    workspace_id = _plan_int(item, "default_workspace_id")
    if not Workspace.objects.filter(pk=workspace_id, organization=organization, is_default=True).exists():
        raise IdentityCutoverConflict("shared organization default workspace is incomplete")
    if Workspace.objects.filter(organization=organization, is_default=True).count() != 1:
        raise IdentityCutoverConflict("shared organization default workspace is ambiguous")


def _verify_planned_membership(item: Mapping[str, object]) -> None:
    """Require the exact principal membership, cleared legacy role and retained owner fact."""
    scope_kind, role = str(item.get("scope_kind", "")), str(item.get("role", ""))
    if legacy_membership_role_blocker(scope_kind, role) is not None:
        raise IdentityCutoverConflict(_LEGACY_MEMBERSHIP_POLICY_DISPOSITION_IS_REQUIRED)
    model = OrganizationMembership if scope_kind == "organization" else WorkspaceMembership
    scope_id = _plan_int(item, "scope_id")
    principal_uuid = _plan_uuid(item, "principal_uuid")
    if not model.objects.filter(
        **{f"{scope_kind}_id": scope_id},
        user__isnull=True,
        principal_uuid=principal_uuid,
        role="",
    ).exists():
        raise IdentityCutoverConflict("planned membership disposition is incomplete")
    if (
        role == "owner"
        and not WorkspaceOwnership.objects.filter(workspace_id=scope_id, principal_uuid=principal_uuid).exists()
    ):
        raise IdentityCutoverConflict("planned workspace owner disposition is incomplete")


def _individual_already_applied(account: Account | None, workspace: Workspace | None, principal_uuid: UUID) -> bool:
    """An exact previously converted personal workspace is an idempotent cutover retry."""
    return (
        account is not None
        and workspace is not None
        and workspace.personal_for_user_id is None
        and workspace.archived_at is not None
        and WorkspaceOwnership.objects.filter(workspace=workspace, principal_uuid=principal_uuid).exists()
    )


def _membership_target(
    scope_kind: str, scope_id: int, principal_uuid: UUID, role: str
) -> tuple[type[OrganizationMembership] | type[WorkspaceMembership], str]:
    """Select the exact retained membership table and preserve only explicit workspace ownership."""
    model: type[OrganizationMembership] | type[WorkspaceMembership]
    if scope_kind == "organization":
        model = OrganizationMembership
        parent_field = "organization_id"
    elif scope_kind == "workspace":
        model = WorkspaceMembership
        parent_field = "workspace_id"
        if role == "owner":
            WorkspaceOwnership.objects.get_or_create(workspace_id=scope_id, principal_uuid=principal_uuid)
    else:
        raise IdentityCutoverConflict("cutover membership scope changed")
    return model, parent_field


def _verify_membership_rows(
    legacy: OrganizationMembership | WorkspaceMembership | None,
    principal: OrganizationMembership | WorkspaceMembership | None,
    role: str,
) -> None:
    """A cutover retry must still find one planned row and the unchanged legacy role."""
    if legacy is None and principal is None:
        raise IdentityCutoverConflict("planned membership changed")
    if legacy is not None and legacy.role != role:
        raise IdentityCutoverConflict("planned membership role changed")


def _membership_administrator_relationship(item: Mapping[str, object]) -> tuple[str, str, str] | None:
    """Emit only the explicitly equivalent organization-admin or workspace-owner grant."""
    role = str(item.get("role", ""))
    scope_kind = str(item.get("scope_kind", ""))
    if legacy_membership_role_blocker(scope_kind, role) is not None:
        raise IdentityCutoverConflict(_LEGACY_MEMBERSHIP_POLICY_DISPOSITION_IS_REQUIRED)
    if (scope_kind, role) not in {("organization", "admin"), ("workspace", "owner")}:
        return None
    model = Organization if scope_kind == "organization" else Workspace
    target_uuid = model.objects.only("uuid").get(pk=_plan_int(item, "scope_id")).uuid
    return (f"principal:{item['principal_uuid']}", "direct_administrator", f"{scope_kind}:{target_uuid}")
