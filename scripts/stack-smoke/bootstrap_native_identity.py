"""Run the real native identity ceremony against the disposable smoke database.

Executed by manage.py shell in a bounded maintenance container. This creates
only the explicit operator identity; the production commands own all cutover
state and all OpenFGA writes. The browser's first-login identity is distinct.
"""

import io
import json
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.management import call_command
from management.services import bind_provider_identity

FACTS = "/smoke-facts/cutover.json"
BOOTSTRAP_FACTS = "/smoke-facts/bootstrap.json"


def main() -> None:
    """Exercise plan, apply, bootstrap, verification and runtime readiness."""
    bootstrap = json.loads(Path(BOOTSTRAP_FACTS).read_text(encoding="utf-8"))
    operator = get_user_model().objects.create_user(
        username="stack-smoke-operator", email="stack-smoke-operator@example.test", password=None
    )
    bind_provider_identity(operator, bootstrap["issuer"], bootstrap["subject"])
    output = io.StringIO()
    call_command("identity_cutover", "plan", facts=FACTS, stdout=output)
    digest = json.loads(output.getvalue())["plan_digest"]
    call_command("identity_cutover", "apply", facts=FACTS, plan_digest=digest)
    call_command("identity_cutover_bootstrap", facts=BOOTSTRAP_FACTS, plan_digest=digest)
    call_command("identity_cutover", "verify", facts=FACTS, plan_digest=digest)
    call_command("identity_runtime_check")


if __name__ == "__main__":
    main()
