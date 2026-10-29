"""Standard DRF error envelope for the platform API."""

from __future__ import annotations

from rest_framework import serializers, status
from rest_framework.exceptions import ErrorDetail, ValidationError
from rest_framework.response import Response
from rest_framework.views import exception_handler as drf_exception_handler

from shared.audit import accepted_request_id
from shared.errors import classify_user_message, safe_user_message

_REQUIRED_FIELD_MESSAGE = "This field is required."
_INVALID_FIELD_MESSAGE = "Invalid value."

_STATUS_MESSAGES = {
    status.HTTP_400_BAD_REQUEST: "Invalid request",
    status.HTTP_401_UNAUTHORIZED: "Authentication failed",
    status.HTTP_403_FORBIDDEN: "Permission denied",
    status.HTTP_404_NOT_FOUND: "Resource not found",
    status.HTTP_405_METHOD_NOT_ALLOWED: "Method not allowed",
    status.HTTP_429_TOO_MANY_REQUESTS: "Request was throttled",
}

_VALIDATION_DETAIL_MESSAGES = {
    "blank": "This field may not be blank.",
    "invalid": _INVALID_FIELD_MESSAGE,
    "max_length": "Value is too long.",
    "min_length": "Value is too short.",
    "null": "This field may not be null.",
    "required": _REQUIRED_FIELD_MESSAGE,
}

_VALIDATION_DETAIL_TEXT_MESSAGES = {
    "Agent name is required": "Agent name is required",
    "Do not supply a password for generated mode.": "Do not supply a password for generated mode.",
    "Either 'agents' or 'agent_id' is required": "Either 'agents' or 'agent_id' is required",
    "Filename is required": "Filename is required",
    "Invalid agent type.": "Invalid agent type.",
    "Invalid credential type.": "Invalid credential type.",
    "Provide at least one of 'enabled' or 'staff_only'.": "Provide at least one of 'enabled' or 'staff_only'.",
    "Request body must be a JSON object": "Request body must be a JSON object",
    _REQUIRED_FIELD_MESSAGE: _REQUIRED_FIELD_MESSAGE,
    "This field is required for set mode.": "This field is required for set mode.",
    "Unknown event status.": "Unknown event status.",
    "Unknown field.": "Unknown field.",
    "Valid file size is required": "Valid file size is required",
    "agent_id is required": "agent_id is required",
    "from_date must not be later than to_date.": "from_date must not be later than to_date.",
    "request_id or range_id is required": "request_id or range_id is required",
}


def api_exception_handler(exc: Exception, context: dict[str, object]) -> Response | None:
    """Wrap DRF exceptions in the platform API error envelope."""
    response = drf_exception_handler(exc, context)
    if response is None:
        return None

    error: dict[str, object] = {
        "code": _error_code(exc),
        "message": _error_message(exc, response),
    }
    if isinstance(exc, ValidationError):
        error["details"] = _normalize_detail(response.data, _validation_serializer(context))
    request_id = _request_id(context.get("request"))
    if request_id:
        error["request_id"] = request_id

    response.data = {"error": error}
    return response


def api_error_response(
    *,
    code: str,
    message: str,
    status_code: int,
    details: object | None = None,
    detail_serializer: serializers.BaseSerializer | None = None,
    request: object | None = None,
) -> Response:
    """Render authored public text; validation keys require their owning serializer.

    Exception messages must be classified by the calling adapter before entering
    this function. Text formatting cannot remove confidential exception content.
    """
    error: dict[str, object] = {
        "code": code,
        "message": safe_user_message(message),
    }
    if details is not None:
        error["details"] = _normalize_detail(details, detail_serializer)
    request_id = _request_id(request)
    if request_id:
        error["request_id"] = request_id
    return Response({"error": error}, status=status_code)


def _error_code(exc: Exception) -> str:
    """Return a stable API error code for a DRF exception."""
    code = getattr(exc, "default_code", None)
    if isinstance(code, str) and code:
        return code
    return "api_error"


def _error_message(exc: Exception, response: Response) -> str:
    """Return the safe user-facing message for an exception response."""
    if isinstance(exc, ValidationError):
        return _STATUS_MESSAGES[status.HTTP_400_BAD_REQUEST]
    # 429 has a known safe status message; do not route DRF's throttle detail
    # ("...Expected available in N seconds") through keyword classification,
    # whose "expected" validation token would mislabel it as "Invalid request".
    if response.status_code in (
        status.HTTP_401_UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN,
        status.HTTP_429_TOO_MANY_REQUESTS,
    ):
        return _STATUS_MESSAGES[response.status_code]

    default = _STATUS_MESSAGES.get(response.status_code, "Request could not be processed")
    return classify_user_message(_extract_detail(response.data), default=default)


def _extract_detail(data: object) -> object:
    """Extract DRF's conventional detail payload for message classification."""
    if isinstance(data, dict) and "detail" in data:
        return data["detail"]
    if isinstance(data, list):
        return " ".join(str(item) for item in data)
    return data


def _validation_serializer(context: dict[str, object]) -> serializers.BaseSerializer | None:
    """Use a view's declared serializer as the source of public field names."""
    serializer_class = getattr(context.get("view"), "serializer_class", None)
    if isinstance(serializer_class, type) and issubclass(serializer_class, serializers.BaseSerializer):
        return serializer_class(context=context)
    return None


def _normalize_detail(data: object, field: serializers.Field | None = None) -> object:
    """Select authored validation text and keys, retaining nested/indexed shape."""
    if isinstance(data, ErrorDetail):
        normalized: object = _VALIDATION_DETAIL_TEXT_MESSAGES.get(
            str(data),
            _VALIDATION_DETAIL_MESSAGES.get(data.code, _INVALID_FIELD_MESSAGE),
        )
    elif isinstance(data, str):
        normalized = _VALIDATION_DETAIL_TEXT_MESSAGES.get(data, _INVALID_FIELD_MESSAGE)
    elif isinstance(data, dict):
        normalized = _normalize_mapping_details(data, field)
    elif isinstance(data, (list, tuple)):
        child = field.child if isinstance(field, (serializers.ListSerializer, serializers.ListField)) else field
        normalized = [_normalize_detail(item, child) for item in data]
    else:
        normalized = _INVALID_FIELD_MESSAGE
    return normalized


def _normalize_mapping_details(data: dict[object, object], field: serializers.Field | None) -> object:
    """List fields expose numeric positions; serializers expose declared names."""
    if isinstance(field, (serializers.ListSerializer, serializers.ListField)):
        return {
            key: _normalize_detail(value, field.child) for key, value in data.items() if type(key) is int and key >= 0
        }
    return _normalize_field_details(data, field)


def _normalize_field_details(data: dict[object, object], field: serializers.Field | None) -> dict[str, object]:
    """Unknown keys may contain exception text; group their errors under a constant."""
    fields = field.fields if isinstance(field, serializers.Serializer) else {}
    normalized = {name: _normalize_detail(data[name], child) for name, child in fields.items() if name in data}
    unknown: list[object] = []
    for key, value in data.items():
        if key not in fields:
            detail = _normalize_detail(value)
            unknown.extend(detail if isinstance(detail, list) else [detail])
    if unknown:
        normalized["non_field_errors"] = unknown
    return normalized


def _request_id(request: object | None) -> str | None:
    """Return a request correlation ID when middleware or headers provide one."""
    if request is None:
        return None
    request_id = accepted_request_id(getattr(request, "request_id", None))
    if request_id:
        return request_id
    meta = getattr(request, "META", {})
    return accepted_request_id(meta.get("HTTP_X_REQUEST_ID")) if isinstance(meta, dict) else None
