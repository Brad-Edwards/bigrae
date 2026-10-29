"""Management admin configuration."""

from django.contrib import admin
from django.contrib.auth import get_user_model
from django.contrib.auth.admin import GroupAdmin, UserAdmin
from django.contrib.auth.models import Group
from django.db.models import Model
from django.http import HttpRequest

from .models import ActivityLog, UserProfile


def _is_active_superuser(request: HttpRequest) -> bool:
    """Return whether the admin session belongs to an active superuser."""
    return bool(request.user.is_active and request.user.is_superuser)


class _SuperuserWritesMixin:
    """Allow add, change, and delete only to an active superuser.

    Django admin lets any staff user who holds the model's change permission
    edit users and groups, including ``is_superuser``, permissions, group
    membership, email, and passwords on their own account. The platform keeps
    the ``auth.change_user`` tier below superuser (the Administer API protects
    superuser targets and makes ownership transfer superuser-only), so identity
    and privilege records are writable in Django admin only by a superuser.
    Other staff keep read access through their view permission.
    """

    def has_add_permission(self, request: HttpRequest) -> bool:
        return _is_active_superuser(request)

    def has_change_permission(self, request: HttpRequest, obj: Model | None = None) -> bool:
        return _is_active_superuser(request)

    def has_delete_permission(self, request: HttpRequest, obj: Model | None = None) -> bool:
        return _is_active_superuser(request)


# django.contrib.auth registers the stock admins first; replace them.
_user_model = get_user_model()
for _model in (_user_model, Group):
    if admin.site.is_registered(_model):
        admin.site.unregister(_model)


@admin.register(_user_model)
class PlatformUserAdmin(_SuperuserWritesMixin, UserAdmin):
    """Stock user admin whose writes, including password changes, need a superuser."""


@admin.register(Group)
class PlatformGroupAdmin(_SuperuserWritesMixin, GroupAdmin):
    """Stock group admin whose writes need a superuser (group permissions grant privilege)."""


@admin.register(UserProfile)
class UserProfileAdmin(_SuperuserWritesMixin, admin.ModelAdmin):
    """Admin for extended user profiles (soft-delete and anonymization state).

    The profile holds the provider identity binding (``cognito_sub``,
    ``issuer``) and lifecycle state that sign-in resolves, so its writes need a
    superuser like the user record itself.
    """

    list_display = ("user", "is_ctf_account", "must_change_password", "deleted_at", "anonymized_at")
    list_filter = ("is_ctf_account", "must_change_password", "deleted_at", "anonymized_at")
    search_fields = ("user__email",)
    raw_id_fields = ("user",)
    readonly_fields = ("is_ctf_account",)


@admin.register(ActivityLog)
class ActivityLogAdmin(admin.ModelAdmin):
    """Read-only admin for the activity/audit log."""

    list_display = ("action", "user", "timestamp")
    list_filter = ("action", "timestamp")
    search_fields = ("action", "user__email")
    raw_id_fields = ("user",)
    readonly_fields = ("action", "user", "timestamp", "metadata")

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj: Model | None = None) -> bool:
        return False
