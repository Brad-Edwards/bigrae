"""Issue-specific SQL conversion for the coordinated S8 identity cutover."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from uuid import NAMESPACE_URL, UUID, uuid5

from django.db import transaction
from django.utils import timezone

from workspaces.identity_mapping import plan_legacy_account_mapping
from workspaces.models import (
    Account,
    AuthorizationGroup,
    AuthorizationOperation,
    AuthorizationPolicy,
    IdentityCutoverState,
    Organization,
    OrganizationMembership,
    Workspace,
    WorkspaceMembership,
    WorkspaceOwnership,
)


class IdentityCutoverConflict(RuntimeError):
    """Persisted facts changed after the cutover plan was created."""


@dataclass(frozen=True, slots=True)
class IdentityCutoverStateView:
    """Persistence-neutral projection of the singleton cutover state."""

    state: str
    plan_digest: str
    plan_payload: dict[str, object]
    relationship_intent: list[dict[str, str]]
    tuple_digest: str
    provider_store_id: str
    model_id: str
    bootstrap_principal_uuid: UUID | None
    bootstrap_verified_at: object | None
    admission_invalidated_at: object | None


def _state_view(state: IdentityCutoverState) -> IdentityCutoverStateView:
    return IdentityCutoverStateView(
        state.state,
        state.plan_digest,
        state.plan_payload,
        state.relationship_intent,
        state.tuple_digest,
        state.provider_store_id,
        state.model_id,
        state.bootstrap_principal_uuid,
        state.bootstrap_verified_at,
        state.admission_invalidated_at,
    )


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
        raise IdentityCutoverConflict("cutover resource plan is invalid")
    for item in individuals:
        if not isinstance(item, Mapping):
            raise IdentityCutoverConflict("cutover resource plan is invalid")
        account = Account.objects.get(individual_principal_uuid=item["principal_uuid"])
        mappings[int(item["legacy_workspace_id"])] = (account.pk, None, None)
    for item in organizations:
        if not isinstance(item, Mapping):
            raise IdentityCutoverConflict("cutover resource plan is invalid")
        organization = Organization.objects.get(pk=int(item["organization_id"]), account__isnull=False)
        for workspace_id in Workspace.objects.filter(organization=organization).values_list("pk", flat=True):
            mappings[workspace_id] = (organization.account_id, organization.pk, workspace_id)
    return mappings


def identity_cutover_relationships(plan: Mapping[str, object]) -> set[tuple[str, str, str]]:
    """Project retained account ancestry and planned administrative grants."""
    relationships: set[tuple[str, str, str]] = set()
    for account_uuid in Account.objects.order_by("uuid").values_list("uuid", flat=True):
        relationships.add(("installation:root", "installation", f"account:{account_uuid}"))
    for organization in Organization.objects.filter(account__isnull=False).select_related("account").order_by("uuid"):
        relationships.add((f"account:{organization.account.uuid}", "account", f"organization:{organization.uuid}"))
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
        role = str(item.get("role", ""))
        scope_kind = str(item.get("scope_kind", ""))
        if (scope_kind, role) not in {("organization", "admin"), ("workspace", "owner")}:
            continue
        model = Organization if scope_kind == "organization" else Workspace
        target_uuid = model.objects.only("uuid").get(pk=int(item["scope_id"])).uuid
        relationships.add(
            (f"principal:{item['principal_uuid']}", "direct_administrator", f"{scope_kind}:{target_uuid}")
        )
    return relationships


def get_identity_cutover_state(*, plan_digest: str | None = None) -> IdentityCutoverStateView | None:
    """Read the singleton state, optionally pinned to one plan digest."""
    query = IdentityCutoverState.objects.filter(key=1)
    if plan_digest is not None:
        query = query.filter(plan_digest=plan_digest)
    state = query.first()
    return _state_view(state) if state is not None else None


def begin_identity_cutover(plan: Mapping[str, object]) -> IdentityCutoverStateView:
    """Create or lock the durable cutover state for one exact plan."""
    digest = str(plan["plan_digest"])
    state, _ = IdentityCutoverState.objects.select_for_update().get_or_create(
        key=1,
        defaults={"state": IdentityCutoverState.State.PLANNED, "plan_digest": digest, "plan_payload": dict(plan)},
    )
    if state.plan_digest != digest:
        raise IdentityCutoverConflict("another cutover plan owns the state")
    return _state_view(state)


def record_identity_cutover_policy_intent(
    plan_digest: str, intent: list[dict[str, str]], tuple_digest: str
) -> IdentityCutoverStateView:
    """Commit the complete relationship intent before provider writes."""
    state = IdentityCutoverState.objects.select_for_update().get(key=1, plan_digest=plan_digest)
    if state.relationship_intent and state.relationship_intent != intent:
        raise IdentityCutoverConflict("cutover relationship intent changed")
    state.state = IdentityCutoverState.State.POLICY_PENDING
    state.relationship_intent = intent
    state.tuple_digest = tuple_digest
    state.save(update_fields=["state", "relationship_intent", "tuple_digest", "updated_at"])
    return _state_view(state)


def bind_identity_cutover_provider(plan_digest: str, store_id: str, model_id: str) -> IdentityCutoverStateView:
    """Pin pending work to the exact policy provider deployment."""
    state = IdentityCutoverState.objects.get(key=1, plan_digest=plan_digest)
    if state.provider_store_id and (state.provider_store_id != store_id or state.model_id != model_id):
        raise IdentityCutoverConflict("authorization provider binding changed")
    if not state.provider_store_id:
        state.provider_store_id = store_id
        state.model_id = model_id
        state.save(update_fields=["provider_store_id", "model_id", "updated_at"])
    return _state_view(state)


def record_identity_cutover_bootstrap(plan_digest: str, principal_uuid: UUID) -> IdentityCutoverStateView:
    """Atomically bind the one verified initial application administrator."""
    state = IdentityCutoverState.objects.select_for_update().get(key=1, plan_digest=plan_digest)
    if state.state != IdentityCutoverState.State.POLICY_PENDING:
        raise IdentityCutoverConflict("cutover policy state changed during bootstrap")
    if state.bootstrap_principal_uuid not in {None, principal_uuid}:
        raise IdentityCutoverConflict("bootstrap principal changed")
    state.bootstrap_principal_uuid = principal_uuid
    state.bootstrap_verified_at = timezone.now()
    state.save(update_fields=["bootstrap_principal_uuid", "bootstrap_verified_at", "updated_at"])
    return _state_view(state)


def activate_identity_cutover(plan_digest: str) -> IdentityCutoverStateView:
    """Activate only the locked, fully verified pending state."""
    state = IdentityCutoverState.objects.select_for_update().get(key=1, plan_digest=plan_digest)
    if state.state == IdentityCutoverState.State.ACTIVATED:
        return _state_view(state)
    if state.state != IdentityCutoverState.State.POLICY_PENDING:
        raise IdentityCutoverConflict("cutover state changed during verification")
    state.state = IdentityCutoverState.State.VERIFIED
    state.save(update_fields=["state", "updated_at"])
    state.admission_invalidated_at = timezone.now()
    state.state = IdentityCutoverState.State.ACTIVATED
    state.save(update_fields=["state", "admission_invalidated_at", "updated_at"])
    return _state_view(state)


def _individual_account_uuid(principal_uuid: UUID) -> UUID:
    return uuid5(NAMESPACE_URL, f"shifter:individual-account:{principal_uuid}")


def _shared_account_uuid(organization_uuid: UUID) -> UUID:
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
        raise IdentityCutoverConflict("cutover plan shape changed")
    with transaction.atomic():
        for item in individuals:
            if not isinstance(item, Mapping):
                raise IdentityCutoverConflict("cutover plan shape changed")
            _apply_individual(item)
        for item in organizations:
            if not isinstance(item, Mapping):
                raise IdentityCutoverConflict("cutover plan shape changed")
            _apply_shared_organization(item)
        for item in memberships:
            if not isinstance(item, Mapping):
                raise IdentityCutoverConflict("cutover plan shape changed")
            _apply_membership(item)


def verify_identity_cutover_hierarchy(plan: Mapping[str, object]) -> None:
    """Verify the applied account/default hierarchy before admission activates."""
    individuals = plan.get("individuals")
    organizations = plan.get("organizations")
    if not isinstance(individuals, Sequence) or not isinstance(organizations, Sequence):
        raise IdentityCutoverConflict("cutover plan shape changed")
    for item in individuals:
        if not isinstance(item, Mapping):
            raise IdentityCutoverConflict("cutover plan shape changed")
        account = Account.objects.filter(individual_principal_uuid=item.get("principal_uuid")).first()
        if account is None or account.kind != Account.Kind.INDIVIDUAL:
            raise IdentityCutoverConflict("individual account hierarchy is incomplete")
        if Organization.objects.filter(account=account).exists():
            raise IdentityCutoverConflict("individual account hierarchy has unexpected subdivisions")
    for item in organizations:
        if not isinstance(item, Mapping):
            raise IdentityCutoverConflict("cutover plan shape changed")
        organization = Organization.objects.filter(pk=item.get("organization_id"), account__isnull=False).first()
        if organization is None or not organization.is_default:
            raise IdentityCutoverConflict("shared account default organization is incomplete")
        if Organization.objects.filter(account_id=organization.account_id, is_default=True).count() != 1:
            raise IdentityCutoverConflict("shared account default organization is ambiguous")
        workspace_id = item.get("default_workspace_id")
        if not Workspace.objects.filter(pk=workspace_id, organization=organization, is_default=True).exists():
            raise IdentityCutoverConflict("shared organization default workspace is incomplete")
        if Workspace.objects.filter(organization=organization, is_default=True).count() != 1:
            raise IdentityCutoverConflict("shared organization default workspace is ambiguous")


def _apply_individual(item: Mapping[str, object]) -> None:
    """Archive one synthetic personal subdivision and preserve its durable facts."""
    try:
        principal_uuid = UUID(str(item["principal_uuid"]))
        user_id = int(item["user_id"])
        organization_id = int(item["legacy_organization_id"])
        workspace_id = int(item["legacy_workspace_id"])
    except (KeyError, TypeError, ValueError) as exc:
        raise IdentityCutoverConflict("cutover plan shape changed") from exc
    organization = Organization.objects.select_for_update().filter(pk=organization_id).first()
    workspace = Workspace.objects.select_for_update().filter(pk=workspace_id, organization_id=organization_id).first()
    existing_account = Account.objects.filter(individual_principal_uuid=principal_uuid).first()
    already_applied = (
        existing_account is not None
        and workspace is not None
        and workspace.personal_for_user_id is None
        and workspace.archived_at is not None
        and WorkspaceOwnership.objects.filter(workspace=workspace, principal_uuid=principal_uuid).exists()
    )
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
        organization_id = int(item["organization_id"])
        workspace_id = int(item["default_workspace_id"])
        account_kind = str(item["account_kind"])
    except (KeyError, TypeError, ValueError) as exc:
        raise IdentityCutoverConflict("cutover plan shape changed") from exc
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
        scope_id = int(item["scope_id"])
        user_id = int(item["user_id"])
        principal_uuid = UUID(str(item["principal_uuid"]))
        role = str(item["role"])
    except (KeyError, TypeError, ValueError) as exc:
        raise IdentityCutoverConflict("cutover plan shape changed") from exc
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
    rows = model.objects.select_for_update().filter(**{parent_field: scope_id})
    legacy = rows.filter(user_id=user_id, principal_uuid__isnull=True).first()
    principal = rows.filter(user_id=None, principal_uuid=principal_uuid).first()
    if legacy is None and principal is None:
        raise IdentityCutoverConflict("planned membership changed")
    if legacy is not None and principal is not None:
        legacy.delete()
    elif legacy is not None:
        legacy.user_id = None
        legacy.principal_uuid = principal_uuid
        legacy.role = ""
        legacy.save(update_fields=["user", "principal_uuid", "role", "updated_at"])
    if rows.filter(user_id=None, principal_uuid=principal_uuid).count() != 1:
        raise IdentityCutoverConflict("planned membership changed")
