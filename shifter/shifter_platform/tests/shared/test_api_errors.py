"""Tests for shared DRF API error-envelope helpers."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from rest_framework import serializers, status
from rest_framework.exceptions import AuthenticationFailed, Throttled, ValidationError

from shared.api.errors import api_error_response, api_exception_handler


class _NameSerializer(serializers.Serializer):
    name = serializers.CharField()


def _context(request_id: str = "req-test", serializer_class=_NameSerializer):
    return {
        "request": SimpleNamespace(META={"HTTP_X_REQUEST_ID": request_id}),
        "view": SimpleNamespace(serializer_class=serializer_class),
    }


def test_validation_errors_use_standard_api_envelope() -> None:
    response = api_exception_handler(
        ValidationError({"name": ["This field is required."]}),
        _context(),
    )

    assert response is not None
    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert response.data == {
        "error": {
            "code": "invalid",
            "message": "Invalid request",
            "details": {"name": ["This field is required."]},
            "request_id": "req-test",
        }
    }


def test_validation_details_do_not_echo_attacker_controlled_exception_text() -> None:
    secret = "db-password=not-for-clients"
    response = api_exception_handler(
        ValidationError({"name": [secret]}),
        _context(),
    )

    assert response is not None
    assert secret not in str(response.data)
    assert response.data["error"]["details"] == {"name": ["Invalid value."]}


def test_validation_details_do_not_echo_unknown_dictionary_keys() -> None:
    secret = "private-backend-credential"
    response = api_exception_handler(ValidationError({secret: [secret]}), _context())

    assert response is not None
    assert secret not in str(response.data)
    assert response.data["error"]["details"] == {"non_field_errors": ["Invalid value."]}


def test_validation_details_preserve_only_exact_authored_messages() -> None:
    response = api_exception_handler(
        ValidationError({"name": ["Agent name is required"]}),
        _context(),
    )

    assert response is not None
    assert response.data["error"]["details"] == {"name": ["Agent name is required"]}


def test_authentication_errors_do_not_echo_raw_exception_text() -> None:
    response = api_exception_handler(AuthenticationFailed("Invalid or expired API token"), _context("req-auth"))

    assert response is not None
    assert response.status_code == status.HTTP_401_UNAUTHORIZED
    assert response.data == {
        "error": {
            "code": "authentication_failed",
            "message": "Authentication failed",
            "request_id": "req-auth",
        }
    }


def test_throttled_errors_render_a_stable_throttle_message() -> None:
    # DRF's Throttled detail ("...Expected available in N seconds") contains the
    # token "expected", which the keyword classifier would otherwise mislabel as
    # a validation error ("Invalid request"). A 429 must render the stable
    # throttle message instead (issue #322 surfaced the first 429 responses).
    response = api_exception_handler(Throttled(wait=60), _context("req-throttle"))

    assert response is not None
    assert response.status_code == status.HTTP_429_TOO_MANY_REQUESTS
    assert response.data == {
        "error": {
            "code": "throttled",
            "message": "Request was throttled",
            "request_id": "req-throttle",
        }
    }


def test_manual_error_response_uses_same_shape() -> None:
    response = api_error_response(
        code="bad_request",
        message="Risk is not deleted",
        status_code=status.HTTP_400_BAD_REQUEST,
    )

    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert response.data == {"error": {"code": "bad_request", "message": "Risk is not deleted"}}


def test_manual_details_do_not_reflect_sensitive_strings_or_keys() -> None:
    secret = "private-backend-credential"
    response = api_error_response(
        code="invalid",
        message="Invalid request",
        status_code=400,
        details={secret: [secret, RuntimeError(secret)]},
    )

    assert secret not in str(response.data)
    assert response.data["error"]["details"] == {"non_field_errors": ["Invalid value.", "Invalid value."]}


def test_validation_details_retain_nested_serializer_and_list_index_shape() -> None:
    class ItemsSerializer(serializers.Serializer):
        children = _NameSerializer(many=True)
        labels = serializers.ListField(child=serializers.CharField())

    serializer = ItemsSerializer(data={"children": [{"name": ""}], "labels": [None]})
    assert not serializer.is_valid()
    response = api_exception_handler(ValidationError(serializer.errors), _context(serializer_class=ItemsSerializer))

    assert response is not None
    assert response.data["error"]["details"] == {
        "children": {0: {"name": ["This field may not be blank."]}},
        "labels": {0: ["This field may not be null."]},
    }


def test_manual_serializer_errors_keep_only_declared_field_names() -> None:
    serializer = _NameSerializer(data={})
    assert not serializer.is_valid()
    response = api_error_response(
        code="invalid",
        message="Invalid request",
        status_code=400,
        details={**serializer.errors, "backend-password": ["internal database credentials"]},
        detail_serializer=serializer,
    )

    assert response.data["error"]["details"] == {
        "name": ["This field is required."],
        "non_field_errors": ["Invalid value."],
    }


@pytest.mark.parametrize(
    "error_kind,expected_code,expected_status,expected_message",
    [
        ("CTFNotFoundError", "not_found", 404, "Resource not found"),
        ("CTFValidationError", "invalid", 400, "Invalid request"),
        ("CTFPermissionError", "permission_denied", 403, "Forbidden"),
        ("CTFStateError", "event_authority_unavailable", 503, "Event authority is unavailable."),
    ],
)
def test_ctf_authority_mapper_does_not_expose_caught_exception_text(
    error_kind, expected_code, expected_status, expected_message
) -> None:
    from ctf import exceptions
    from ctf.api.organizer.staff import _authority_error

    exception_type = getattr(exceptions, error_kind)
    secret = "private-backend-credential"
    try:
        raise exception_type(f"Traceback (most recent call last): {secret}", details={secret: secret})
    except exception_type as exc:
        response = _authority_error(exc).to_response(_context()["request"])

    assert secret not in str(response.data)
    assert "Traceback" not in str(response.data)
    assert response.status_code == expected_status
    assert response.data["error"]["code"] == expected_code
    assert response.data["error"]["message"] == expected_message


def test_pack_blocker_details_contain_only_public_states_and_counts() -> None:
    from cms.api.tenant_packs import _removal_error
    from cms.services import PackRemovalConflict
    from cms.services._pack_references import PackBlocker

    secret = "private-backend-credential"
    error = PackRemovalConflict(
        "pack_in_use",
        "The pack is still in use by 3 active reference(s)",
        blockers=(
            PackBlocker("range", "ready", secret),
            PackBlocker("ctf_event", secret, secret),
            PackBlocker(secret, secret, secret),
        ),
        total=3,
    )
    response = _removal_error(_context()["request"], error)

    assert secret not in str(response.data)
    assert response.status_code == 409
    assert response.data["error"]["details"] == {
        "blocker_count": 3,
        "blockers": [
            {"kind": "range", "status": "ready", "label": None},
            {"kind": "ctf_event", "status": "unavailable", "label": None},
            {"kind": "resource", "status": "unavailable", "label": None},
        ],
    }
