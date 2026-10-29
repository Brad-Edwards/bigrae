"""Formatting for authored public errors and classification of caught failures.

``safe_user_message`` and ``UserFacingError`` limit length and remove line
breaks from text that is already intended for clients. They do not remove
credentials, traceback content, or other confidential exception information.

Caught failures must instead be mapped to fixed public messages, either in the
owning API adapter or through ``classify_user_message``. Classification selects
authored text without returning the original exception message.
"""

from __future__ import annotations

_MAX_MESSAGE_LEN = 500
_DEFAULT_MESSAGE = "An error occurred"


class UserFacingError(Exception):
    """Carry bounded, formatted text that its caller has authored for clients."""

    def __init__(self, user_message: str, *, http_status: int = 400) -> None:
        clean = self._sanitize(user_message)
        super().__init__(clean)
        self.user_message = clean
        self.http_status = http_status

    @staticmethod
    def _sanitize(message: object) -> str:
        """Bound and format already-public text without classifying its content.

        Remove line breaks and surrounding whitespace, limit the displayed
        length, and replace empty input with a generic message. Confidential
        content within the remaining text is unchanged.
        """
        if message is None:
            return _DEFAULT_MESSAGE
        text = str(message).replace("\r", " ").replace("\n", " ").strip()
        if not text:
            return _DEFAULT_MESSAGE
        return text[:_MAX_MESSAGE_LEN]


def safe_user_message(message: object) -> str:
    """Format already-public text; use classification for caught exception text."""
    return UserFacingError(str(message) if message is not None else "").user_message


# ---------------------------------------------------------------------------
# Category-based message selection
# ---------------------------------------------------------------------------
#
# CodeQL's ``py/stack-trace-exposure`` taint flow is not severed by routing
# ``str(exc)`` through :class:`UserFacingError`'s CR/LF stripping — the rule
# considers the message still tainted by the original exception. The only
# reliable way to clear the taint is to return a value selected from a fixed
# set of authored string literals. ``classify_user_message`` inspects the
# original message for known substrings and returns one of those literals,
# defaulting to a generic "request could not be processed" message.
#
# Call sites should still ``logger.exception(...)`` the full error so the
# specific text is preserved in logs.


_NOT_FOUND_TOKENS = ("not found", "does not exist", "no such", "missing")
_PERMISSION_TOKENS = ("permission", "forbidden", "not allowed", "access denied", "unauthorized")
_NOT_ACCESSIBLE_TOKENS = ("not accessible", "not ready", "not available", "wrong state")
_NOT_CONFIGURED_TOKENS = ("not configured",)
# More specific than the generic "unavailable" class: a pack that resolves to a
# workspace in a different organization than the one it was installed in. Must be
# matched ahead of ``_UNAVAILABLE_TOKENS`` so operators get the actionable
# organization-scope message instead of a misleading "Service is unavailable".
_PACK_WORKSPACE_ORG_TOKENS = ("different organization",)
_UNAVAILABLE_TOKENS = ("unavailable",)
_CONFLICT_TOKENS = ("already exists", "already have", "duplicate", "conflict", "in progress")
_VALIDATION_TOKENS = ("invalid", "must be", "required", "too large", "too long", "exceeds", "expected")

_EXACT_CLASSIFICATIONS = {
    "authenticated user id unavailable": "Authenticated user id unavailable",
    "failed to generate rdp url": "Failed to generate RDP URL",
    "failed to generate ssh url": "Failed to generate SSH URL",
    "guacamole session request expired": "Guacamole session request expired",
    "internal server error": "Internal server error",
    "ngfw ssh unavailable": "NGFW SSH unavailable",
    "range ssh unavailable": "Range SSH unavailable",
    "rdp connection unavailable": "RDP connection unavailable",
    "rdp service not configured": "RDP service not configured",
    "ssh service not configured": "SSH service not configured",
}


# Ordered list of (token-set, hardcoded-response) pairs. Order matters:
# the first matching token-set wins so callers get the most specific class.
_CLASSIFICATION_RULES: tuple[tuple[tuple[str, ...], str], ...] = (
    (_NOT_FOUND_TOKENS, "Resource not found"),
    (_PERMISSION_TOKENS, "Permission denied"),
    (_NOT_CONFIGURED_TOKENS, "Service is not configured"),
    (_PACK_WORKSPACE_ORG_TOKENS, "This pack is not available in the selected workspace's organization"),
    (_UNAVAILABLE_TOKENS, "Service is unavailable"),
    (_NOT_ACCESSIBLE_TOKENS, "Resource is not accessible in its current state"),
    (_CONFLICT_TOKENS, "Request conflicts with current state"),
    (_VALIDATION_TOKENS, "Invalid request"),
)


def _match_classification(text: str) -> str | None:
    """Return the first hardcoded classification whose tokens are present, else ``None``."""
    for tokens, label in _CLASSIFICATION_RULES:
        if any(tok in text for tok in tokens):
            return label
    return None


def classify_user_message(
    message: object,
    *,
    default: str = "Request could not be processed",
) -> str:
    """Return a hardcoded user-facing string chosen from the original message.

    The returned value is always one of a small fixed set of string literals
    defined in this module, so the call site's response body is not tainted
    by the original (potentially attacker-influenced) exception text. The
    classification is best-effort keyword matching; on no match, ``default``
    (which must itself be a literal supplied at the call site) is returned.

    The full original message should be sent to the logger via
    ``logger.exception`` separately — this helper is only for what's surfaced
    to the API caller.
    """
    text = ("" if message is None else str(message)).lower()
    label = _EXACT_CLASSIFICATIONS.get(text)
    if label is None and text:
        label = _match_classification(text)
    return label if label is not None else default
