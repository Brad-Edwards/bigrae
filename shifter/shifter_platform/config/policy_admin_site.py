"""Production read-only Django admin composed from policy-aware registrations."""

from management.services import policy_admin_registrations as management_admin_registrations
from shared.admin_policy import PolicyAdminSite, PolicyReadOnlyAdmin
from shared.api_tokens.models import ApiToken
from workspaces.services import policy_admin_registrations as workspace_admin_registrations


class PolicyApiTokenAdmin(PolicyReadOnlyAdmin):
    """Expose safe credential metadata without verifier or mutation access."""

    policy_action = "installation.manage_credentials"
    fields = (
        "name",
        "display_id",
        "scopes",
        "target_type",
        "target_uuid",
        "created_by",
        "created_at",
        "last_used_at",
        "expires_at",
        "revoked_at",
        "reissue_required_at",
    )
    readonly_fields = fields


policy_admin_site = PolicyAdminSite(name="policy_admin")
for model, admin_class in (*management_admin_registrations(), *workspace_admin_registrations()):
    policy_admin_site.register(model, admin_class)
policy_admin_site.register(ApiToken, PolicyApiTokenAdmin)
