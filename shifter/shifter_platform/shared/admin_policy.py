"""Read-only Django-admin boundary prepared for the coordinated policy cutover."""

from typing import TYPE_CHECKING, cast

from django.contrib import admin
from django.core.exceptions import PermissionDenied

from shared.api.principals import authenticated_credential
from shared.authorization import AuthorizationRequest, TargetRef, configured_authorization_provider
from shared.identity_scope import ResourceScope
from shared.principal_port import resolve_principal

if TYPE_CHECKING:
    from rest_framework.request import Request


class PolicyReadOnlyAdmin(admin.ModelAdmin):
    """Canonical API/services own mutations; retained admin inspection uses policy."""

    policy_action = "installation.manage_principals"
    actions = None

    def _credential(self, request):
        actor = authenticated_credential(cast("Request", request))
        if actor.kind == "temporary":
            raise PermissionDenied
        resolve_principal(actor.principal)
        return actor

    def _allowed(self, request, obj=None):
        actor = self._credential(request)
        return (
            configured_authorization_provider()
            .check(
                AuthorizationRequest(
                    actor.principal,
                    self.policy_action,
                    TargetRef("installation"),
                    ResourceScope("installation"),
                    actor.ceiling,
                )
            )
            .allowed
        )

    def has_view_permission(self, request, obj=None):
        try:
            return self._allowed(request, obj)
        except Exception:
            return False

    def has_module_permission(self, request):
        return self.has_view_permission(request)

    def get_queryset(self, request):
        if not self.has_view_permission(request):
            raise PermissionDenied
        return super().get_queryset(request)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def save_model(self, request, obj, form, change):
        raise PermissionDenied

    def delete_model(self, request, obj):
        raise PermissionDenied

    def delete_queryset(self, request, queryset):
        raise PermissionDenied


class PolicyAdminSite(admin.AdminSite):
    """Dormant site admission; registered policy admins authorize every read."""

    def has_permission(self, request):
        try:
            actor = authenticated_credential(cast("Request", request))
            if actor.kind == "temporary":
                return False
            return resolve_principal(actor.principal) == actor.principal
        except Exception:
            return False
