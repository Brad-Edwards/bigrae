"""CMS-owned action discovery delegates scalar hierarchy selection to Workspaces."""

from shared.authorization import configured_authorization_provider
from shared.credentials import CredentialContext
from workspaces.services import ContentScopePage, list_content_scope_options

from ._content_authority import ContentAuthorityError


def content_scope_options(credential: CredentialContext, *, offset: int = 0, limit: int = 50) -> ContentScopePage:
    """Expose only independently authorized content collection destinations."""
    try:
        return list_content_scope_options(credential, configured_authorization_provider(), offset=offset, limit=limit)
    except Exception:
        raise ContentAuthorityError("Content scopes unavailable") from None
