"""Issue real Knox proof through the owning service using a real operator login.

The browser-established session arrives on stdin; the bearer is written once to
a private file, never printed. No credential context or session is fabricated.
"""

import os
import sys
from datetime import timedelta

from config.session_credentials import session_credential
from django.contrib.auth import get_user_model
from django.utils import timezone
from management.personal_credentials import issue_personal_token
from management.session_backend import SessionStore
from shared.authorization import TargetRef
from shared.identity_scope import ResourceScope


def main() -> None:
    """Admit the actual operator session and each requested live action."""
    session = SessionStore(session_key=sys.stdin.read().strip())
    user = get_user_model().objects.get(username="stack-smoke-operator")
    _token, raw = issue_personal_token(
        actor=session_credential(user, session),
        user=user,
        name="Synthetic stack smoke catalog setup",
        scopes=[f"authorization:installation.{action}_content" for action in ("manage", "read", "publish")],
        expires_at=timezone.now() + timedelta(minutes=15),
        target=TargetRef("installation"),
        scope=ResourceScope("installation"),
    )
    descriptor = os.open("/run/catalog-credential", os.O_WRONLY | os.O_TRUNC | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(raw)


if __name__ == "__main__":
    main()
