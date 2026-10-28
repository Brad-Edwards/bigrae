"""Purpose identities for the GitHub-less GCP deploy commands (#2571, ADR-070).

A GCP deployment has three lifecycle purpose identities: deploy, release scan, and
destroy. Without GitHub, an operator authenticates with their own Google identity and
the commands impersonate one purpose identity per stage. The impersonation travels
through the documented environment variables of each tool, so no service-account key
is created and every child process uses exactly the selected identity:

- ``CLOUDSDK_AUTH_IMPERSONATE_SERVICE_ACCOUNT`` for ``gcloud``, the GKE auth plugin, and
  the Docker credential helper;
- ``GOOGLE_IMPERSONATE_SERVICE_ACCOUNT`` for the Terraform Google provider;
- ``GOOGLE_BACKEND_IMPERSONATE_SERVICE_ACCOUNT`` for the Terraform ``gcs`` backend.

In ``ambient`` mode (CI), the job credentials already are the purpose identity.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

IdentityMode = Literal["impersonate", "ambient"]
Purpose = Literal["deploy", "release_scan", "destroy"]

PURPOSES: tuple[Purpose, ...] = ("deploy", "release_scan", "destroy")
# Account-id suffixes of the cicd-oidc-identity module (platform/terraform/gcp).
_ACCOUNT_SUFFIX: dict[Purpose, str] = {"deploy": "-deploy", "release_scan": "-scan", "destroy": "-destroy"}
_SERVICE_ACCOUNT_RE = re.compile(
    r"^[a-z][a-z0-9-]{4,28}[a-z0-9]@[a-z][a-z0-9-]{4,28}[a-z0-9]\.iam\.gserviceaccount\.com$"
)
_NAME_PREFIX_RE = re.compile(r"^[a-z][a-z0-9-]*[a-z0-9]$")
_IMPERSONATION_KEYS = (
    "CLOUDSDK_AUTH_IMPERSONATE_SERVICE_ACCOUNT",
    "GOOGLE_IMPERSONATE_SERVICE_ACCOUNT",
    "GOOGLE_BACKEND_IMPERSONATE_SERVICE_ACCOUNT",
)


class IdentityError(ValueError):
    """A purpose identity is malformed or cannot be derived."""


def derived_service_account(project_id: str, environment: str, purpose: Purpose, *, name_prefix: str = "") -> str:
    """Return the purpose identity email from the ``shifter-<environment>`` naming contract."""
    prefix = name_prefix or f"shifter-{environment}"
    if not _NAME_PREFIX_RE.fullmatch(prefix):
        raise IdentityError("name prefix must use lowercase letters, digits, and internal hyphens")
    account_id = prefix.replace("-", "") + _ACCOUNT_SUFFIX[purpose]
    email = f"{account_id}@{project_id}.iam.gserviceaccount.com"
    if not _SERVICE_ACCOUNT_RE.fullmatch(email):
        raise IdentityError(
            f"derived {purpose} identity {email!r} is not a valid service account; pass the identity explicitly"
        )
    return email


def validate_service_account(email: str) -> str:
    """Return ``email`` when it is a user-managed service account in a project."""
    if not _SERVICE_ACCOUNT_RE.fullmatch(email):
        raise IdentityError(f"{email!r} is not a user-managed service account email")
    return email


@dataclass(frozen=True)
class PurposeIdentities:
    """The selected identity mode and the three purpose identities of one deployment."""

    mode: IdentityMode
    deploy: str
    release_scan: str
    destroy: str

    @classmethod
    def resolve(
        cls,
        *,
        mode: IdentityMode,
        project_id: str,
        environment: str,
        name_prefix: str = "",
        deploy: str = "",
        release_scan: str = "",
        destroy: str = "",
    ) -> PurposeIdentities:
        """Resolve explicit identities, deriving any that the caller left empty."""
        explicit = {"deploy": deploy, "release_scan": release_scan, "destroy": destroy}
        resolved: dict[str, str] = {}
        for purpose in PURPOSES:
            value = explicit[purpose]
            resolved[purpose] = (
                validate_service_account(value)
                if value
                else derived_service_account(project_id, environment, purpose, name_prefix=name_prefix)
            )
        return cls(mode=mode, **resolved)

    def email(self, purpose: Purpose) -> str:
        """Return the identity email of one purpose."""
        return {"deploy": self.deploy, "release_scan": self.release_scan, "destroy": self.destroy}[purpose]

    def env(self, purpose: Purpose) -> dict[str, str | None]:
        """Return the child environment overlay that selects ``purpose``.

        Ambient mode removes the impersonation keys, so an inherited impersonation
        setting can never select a different identity.
        """
        if self.mode == "ambient":
            return dict.fromkeys(_IMPERSONATION_KEYS)
        email = self.email(purpose)
        return dict.fromkeys(_IMPERSONATION_KEYS, email)
