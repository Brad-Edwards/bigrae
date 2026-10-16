"""Structural installer projections consumed during range hydration."""

from typing import Protocol


class _AgentOS(Protocol):
    """Minimum operating-system projection required during template hydration."""

    slug: str


class _AgentSource(Protocol):
    """Minimum agent projection required during template hydration."""

    os: _AgentOS
    s3_key: str
    original_filename: str
    sha256_hash: str | None
