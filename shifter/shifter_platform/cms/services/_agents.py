"""Native principal entrypoints for installer custody and projections."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from cms.exceptions import CMSError
from shared.identity_scope import ResourceScope

from ._content_authority import require_native_content_credential

if TYPE_CHECKING:
    from cms.models import AgentConfig
    from shared.schemas.cms_projections import AgentListItem


def create_agent(user: object, **kwargs: Any) -> AgentConfig:
    """Install only bytes validated under a recorded native upload admission."""
    from ._scoped_uploads import complete_scoped_upload

    credential = require_native_content_credential(user)
    if set(kwargs) != {"upload_token"}:
        raise CMSError("A verified upload is required")
    return complete_scoped_upload(credential, kwargs["upload_token"])


def delete_agent(user: object, agent_id: int) -> None:
    """Retire an installer only under current native content authority."""
    from ._scoped_agents import delete_scoped_agent

    return delete_scoped_agent(require_native_content_credential(user), agent_id)


def list_agents(user: object, *, scope: ResourceScope | None = None) -> list[AgentListItem]:
    """List installers in the explicitly authorized native scope."""
    from ._scoped_agents import list_scoped_agents

    return list_scoped_agents(require_native_content_credential(user), scope)


def get_agent(user: object, agent_id: int) -> AgentConfig:
    """Resolve one installer under current native custody."""
    from ._scoped_agents import get_scoped_agent

    return get_scoped_agent(require_native_content_credential(user), agent_id)


def get_allowed_extensions() -> list[str]:
    """Public upload metadata shares the owning validator's extension policy."""
    from cms.assets.validation import get_allowed_extensions as extensions

    return extensions()


def max_agent_file_size_bytes() -> int:
    """Expose the owning validator's upload-size limit."""
    from cms.assets.validation import agent_max_file_size_bytes

    return agent_max_file_size_bytes()
