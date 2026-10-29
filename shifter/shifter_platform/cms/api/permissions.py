"""CMS DRF permissions and actor resolution."""

from __future__ import annotations

from typing import TYPE_CHECKING

from rest_framework import permissions
from rest_framework.exceptions import PermissionDenied

from shared.api.permissions import IsAuthenticatedSessionOrApiToken
from shared.api.principals import authenticated_credential
from shared.credentials import CredentialContext, resolve_current_credential
from shared.principal_port import PrincipalResolutionError

if TYPE_CHECKING:
    from rest_framework.request import Request
    from rest_framework.views import APIView

PermissionClass = type[permissions.BasePermission]


def cms_actor_credential(request: Request) -> CredentialContext:
    """Return the live proof admitted at HTTP ingress without substituting a user."""
    try:
        return resolve_current_credential(authenticated_credential(request))
    except (ValueError, PrincipalResolutionError):
        raise PermissionDenied("Content authority unavailable") from None


class HasCurrentCMSCredential(permissions.BasePermission):
    """Authenticate one live principal; owning content services enforce policy."""

    message = "Forbidden"

    def has_permission(self, request: Request, view: APIView) -> bool:
        try:
            cms_actor_credential(request)
            return True
        except ValueError:
            return False


CMS_READ_PERMISSIONS: list[PermissionClass] = [
    IsAuthenticatedSessionOrApiToken,
    HasCurrentCMSCredential,
]
CMS_WRITE_PERMISSIONS: list[PermissionClass] = [
    IsAuthenticatedSessionOrApiToken,
    HasCurrentCMSCredential,
]
