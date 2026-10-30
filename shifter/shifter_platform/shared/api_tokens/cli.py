"""Private-file CLI transport for the existing bearer authenticator."""

import os
import stat
from pathlib import Path

from django.http import HttpRequest
from rest_framework.exceptions import AuthenticationFailed
from rest_framework.request import Request

from shared.api_tokens.authentication import ApiTokenAuthentication
from shared.credentials import CredentialContext

_UNAVAILABLE = "Native credential file is unavailable or authentication failed"
_MAX_BYTES = 16384


class CredentialFileError(ValueError):
    """One bounded error; paths, proof bytes and provider diagnostics stay private."""


def credential_from_proof(proof: str) -> CredentialContext:
    """Authenticate one bearer proof through the existing authenticator."""
    try:
        if not proof or len(proof) > _MAX_BYTES or any(character.isspace() for character in proof):
            raise CredentialFileError(_UNAVAILABLE)
        raw_request = HttpRequest()
        raw_request.META["HTTP_AUTHORIZATION"] = f"Bearer {proof}"
        request = Request(raw_request)
        ApiTokenAuthentication().authenticate(request)
        credential = getattr(request, "credential_context", None)
        if not isinstance(credential, CredentialContext):
            raise CredentialFileError(_UNAVAILABLE)
        return credential
    except AuthenticationFailed:
        raise CredentialFileError(_UNAVAILABLE) from None


def credential_from_file(path: str | Path) -> CredentialContext:
    """Authenticate actual bearer proof, never a credential reference or a username."""
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as handle:
            metadata = os.fstat(handle.fileno())
            if (
                not stat.S_ISREG(metadata.st_mode)
                or stat.S_IMODE(metadata.st_mode) != 0o600
                or metadata.st_uid != os.geteuid()
                or metadata.st_size > _MAX_BYTES
            ):
                raise CredentialFileError(_UNAVAILABLE)
            proof = handle.read(_MAX_BYTES + 1).decode("ascii").strip()
    except (OSError, UnicodeError):
        raise CredentialFileError(_UNAVAILABLE) from None
    return credential_from_proof(proof)
