"""Retired CTF notification email helpers.

Houses the retired low-level email choke point (``_send_email``) and the
tokenless CTF login URL builder (``_build_ctf_login_url``). Ledger email renders
through the communication email adapter (#580).
"""

from __future__ import annotations

from ctf.services.notification._scheduled import retired_write

_send_email = retired_write


def _build_ctf_login_url() -> str:
    """Build the tokenless dedicated CTF participant login URL."""
    from django.conf import settings
    from django.urls import reverse

    path = reverse("ctf:ctf_login")
    base = (getattr(settings, "SITE_URL", "") or "").rstrip("/")
    return f"{base}{path}"
