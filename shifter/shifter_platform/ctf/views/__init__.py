"""Server-rendered CTF participant authentication views.

The SPA renders every other CTF page, and ``/api/v1/ctf/`` serves workspace
reads and mutations. ``ctf/urls.py`` reaches the login and password-change
views as ``views.<name>``. The private ``_access`` and ``_parsing`` submodules
hold helpers that ``ctf.api`` imports directly.
"""

from __future__ import annotations

from ctf.views.participant_auth import ctf_change_password, ctf_login

__all__ = ["ctf_change_password", "ctf_login"]
