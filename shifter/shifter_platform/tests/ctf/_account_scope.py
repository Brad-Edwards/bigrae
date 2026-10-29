"""Synthetic direct-account placement helpers for CTF tests."""

from shared.principal_port import principal_for_user
from workspaces.models import Account


def individual_account_uuid(user) -> str:
    """Return the fixture user's explicit individual account UUID."""
    principal = principal_for_user(user)
    account, _ = Account.objects.get_or_create(
        individual_principal_uuid=principal.uuid,
        defaults={"kind": Account.Kind.INDIVIDUAL, "name": f"Test account {user.pk}"},
    )
    return str(account.uuid)


def individual_account_scope_fields(user) -> dict[str, object]:
    """Return scalar fields for a direct individual-account resource scope."""
    principal = principal_for_user(user)
    account, _ = Account.objects.get_or_create(
        individual_principal_uuid=principal.uuid,
        defaults={"kind": Account.Kind.INDIVIDUAL, "name": f"Test account {user.pk}"},
    )
    return {
        "scope_kind": "account",
        "account_id": account.pk,
        "organization_id": None,
        "workspace_id": None,
    }
