"""Request-body parsing helpers and bracket-filter resolution."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any
from uuid import UUID

if TYPE_CHECKING:
    from ctf.models import (
        CTFBracket,
    )

logger = logging.getLogger(__name__)


class _BodyUUIDError(ValueError):
    """Raised by `_parse_body_uuid` for missing or malformed request-body UUIDs.

    Used to keep the JSON API error envelope consistent (400 with a
    `{"error": ...}` payload) when a caller posts a non-UUID value. Caught
    locally; never propagates out of a view.
    """


def _parse_body_uuid(value: object, field_name: str) -> UUID:
    """Parse a request-body UUID, raising `_BodyUUIDError` on failure.

    Centralizes the "convert a string from `request.body` JSON into a UUID"
    step so every JSON API endpoint maps malformed values to a 400 response
    instead of leaking `ValueError` / `TypeError` up to Django and returning
    500. Views call this and `except _BodyUUIDError` to render a normal
    400 envelope.
    """
    if not isinstance(value, str) or not value:
        raise _BodyUUIDError(f"{field_name} is required")
    try:
        return UUID(value)
    except (ValueError, TypeError) as e:
        raise _BodyUUIDError(f"Invalid {field_name}") from e


class _BodyParseError(ValueError):
    """Raised for a malformed field in a parsed JSON request body."""


def _get_body_str(body: dict[str, Any], field_name: str, *, default: str = "", required: bool = False) -> str:
    """Pull a string-typed field from a parsed body, raising _BodyParseError on type mismatch.

    Codex review (cycle 6): JSON callers passing `null`, an integer, an
    array, etc. for a field the view treats as a string previously caused
    `AttributeError` on `.strip()` and a 500. Centralise the type check
    so every endpoint produces the same 400 envelope.
    """
    if field_name not in body:
        if required:
            raise _BodyParseError(f"{field_name} is required")
        return default
    value = body[field_name]
    if value is None:
        if required:
            raise _BodyParseError(f"{field_name} is required")
        return default
    if not isinstance(value, str):
        raise _BodyParseError(f"{field_name} must be a string")
    return value


def _resolve_bracket_filter(
    event_id: UUID, bracket_param: str | None
) -> tuple[list[CTFBracket], CTFBracket | None, UUID | None]:
    """Resolve bracket filter from query parameter.

    Args:
        event_id: UUID of the event.
        bracket_param: Raw bracket ID string from request.GET.get("bracket").

    Returns:
        Tuple of (brackets list, selected_bracket object or None, bracket_id UUID or None).
    """
    from ctf.services.bracket import list_brackets

    brackets = list(list_brackets(event_id))
    selected_bracket = None
    bracket_id = None
    if bracket_param:
        for b in brackets:
            if str(b.id) == bracket_param:
                selected_bracket = b
                bracket_id = b.id
                break
    return brackets, selected_bracket, bracket_id
