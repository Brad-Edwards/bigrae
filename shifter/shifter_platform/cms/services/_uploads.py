"""Native signed-upload entrypoints; every capability retains durable custody."""

from __future__ import annotations

from typing import TYPE_CHECKING

from shared.identity_scope import ResourceScope

from ._content_authority import require_native_content_credential

if TYPE_CHECKING:
    from cms.models import AgentConfig
    from shared.schemas.cms_projections import UploadInitiation


def initiate_upload(
    user: object,
    name: str,
    filename: str,
    file_size: int,
    agent_type: str = "xdr",
    *,
    scope: ResourceScope | None = None,
) -> UploadInitiation:
    """Reserve scoped upload custody before issuing a storage capability."""
    from ._scoped_uploads import initiate_scoped_upload

    return initiate_scoped_upload(require_native_content_credential(user), scope, name, filename, file_size, agent_type)


def complete_upload(user: object, upload_token: str) -> AgentConfig:
    """Install only bytes retained by a live native upload admission."""
    from ._scoped_uploads import complete_scoped_upload

    return complete_scoped_upload(require_native_content_credential(user), upload_token)


def cancel_upload(user: object, upload_token: str) -> None:
    """Cancel the exact native upload without browser-derived authority."""
    from ._scoped_uploads import cancel_scoped_upload

    return cancel_scoped_upload(require_native_content_credential(user), upload_token)


def get_storage_used(user: object, *, scope: ResourceScope | None = None) -> int:
    """Project storage usage only within the authorized native scope."""
    from ._scoped_agents import scoped_storage_used

    return scoped_storage_used(require_native_content_credential(user), scope)
