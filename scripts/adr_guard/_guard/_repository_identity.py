"""The upstream repository identity declaration (ADR-069).

`docs/adr/repository-identity.json` is the one place that names the upstream
repository and its immutable IDs. The workflow expression model and the
`repository-identity-literals` check read it; no guard module repeats the values.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from ._common import REPO_ROOT

REPOSITORY_IDENTITY_REL = "docs/adr/repository-identity.json"
_REPOSITORY_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9._-]{1,100}$")
_ID_RE = re.compile(r"^[1-9][0-9]*$")


class RepositoryIdentityError(Exception):
    """The declaration is missing or malformed. The guard fails closed."""


@dataclass(frozen=True)
class RepositoryIdentity:
    """The upstream repository name, owner, immutable IDs, and former names."""

    repository: str
    repository_id: str
    owner: str
    owner_id: str
    former_repositories: tuple[str, ...]


def _required(data: dict, key: str, pattern: re.Pattern[str]) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise RepositoryIdentityError(f"{REPOSITORY_IDENTITY_REL}: '{key}' is missing or malformed")
    return value


def load_repository_identity(repo_root: Path = REPO_ROOT) -> RepositoryIdentity:
    """Read and validate the declaration under `repo_root`."""
    path = repo_root / REPOSITORY_IDENTITY_REL
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RepositoryIdentityError(f"cannot read {REPOSITORY_IDENTITY_REL}: {exc}") from exc
    if not isinstance(data, dict):
        raise RepositoryIdentityError(f"{REPOSITORY_IDENTITY_REL} must be a JSON object")
    repository = _required(data, "repository", _REPOSITORY_RE)
    owner = data.get("owner")
    if owner != repository.split("/", 1)[0]:
        raise RepositoryIdentityError(f"{REPOSITORY_IDENTITY_REL}: 'owner' must equal the owner part of 'repository'")
    former = data.get("former_repositories", [])
    if not isinstance(former, list) or not all(isinstance(f, str) and _REPOSITORY_RE.fullmatch(f) for f in former):
        raise RepositoryIdentityError(f"{REPOSITORY_IDENTITY_REL}: 'former_repositories' must list owner/repo names")
    return RepositoryIdentity(
        repository=repository,
        repository_id=_required(data, "repository_id", _ID_RE),
        owner=owner,
        owner_id=_required(data, "owner_id", _ID_RE),
        former_repositories=tuple(former),
    )
