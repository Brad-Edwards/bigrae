"""CTF forms.

The SPA and ``/api/v1/ctf/`` own event, challenge, participant and
notification management. The only server-rendered CTF form left is the public
self-registration form.
"""

from __future__ import annotations

from ._public_registration import PublicRegistrationForm

__all__ = ("PublicRegistrationForm",)
