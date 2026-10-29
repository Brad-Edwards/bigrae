"""Django admin sign-in goes through the identity provider (#2617).

The stock ``/admin/login/`` form signs in a local password account with a
password only, and the session it creates is valid on the whole portal. Provider
sign-in (Cognito or Identity Platform) requires MFA. Staff therefore reach
Django admin only with a provider session: the admin login URL sends the
browser to the platform login and never checks a password.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.http import HttpResponseRedirect
from django.urls import reverse
from django.views.decorators.http import require_safe

if TYPE_CHECKING:
    from django.http import HttpRequest


@require_safe
def admin_login(request: HttpRequest) -> HttpResponseRedirect:
    """Send Django admin sign-in to the provider-backed platform login."""
    return HttpResponseRedirect(reverse("platform_login"))
