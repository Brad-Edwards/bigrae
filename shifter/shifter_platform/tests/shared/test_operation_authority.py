"""Durable admission carries registered non-secret references and exact ceilings."""

from types import SimpleNamespace
from uuid import uuid4

import pytest

from shared.authorization import CredentialCeiling, TargetRef
from shared.credentials import CredentialContext
from shared.identity_scope import PrincipalRef
from shared.operation_authority import credential_reference_fields, credential_reference_from_record


def test_session_reference_roundtrip_keeps_empty_http_scopes_and_exact_action_target():
    credential = CredentialContext(
        PrincipalRef(uuid4(), "human"),
        "session",
        uuid4(),
        CredentialCeiling(frozenset({"range.read"}), TargetRef("range", uuid4())),
        frozenset(),
    )
    fields = credential_reference_fields(credential)
    assert credential_reference_from_record(SimpleNamespace(**fields)) == credential
    assert set(fields) == {
        "initiating_principal_uuid",
        "initiating_principal_kind",
        "credential_kind",
        "credential_uuid",
        "credential_ceiling_actions",
        "credential_target_type",
        "credential_target_uuid",
        "credential_scopes",
        "credential_event_uuid",
    }


def test_unknown_scope_or_noncanonical_record_cannot_carry_secret_material():
    credential = CredentialContext(
        PrincipalRef(uuid4(), "service"),
        "service",
        uuid4(),
        CredentialCeiling(frozenset({"range.manage"})),
        frozenset({"SECRET_MARKER"}),
    )
    with pytest.raises(ValueError):
        credential_reference_fields(credential)
    good = CredentialContext(
        credential.principal,
        "service",
        credential.credential_uuid,
        credential.ceiling,
        frozenset({"authorization:range.manage"}),
    )
    fields = credential_reference_fields(good)
    fields["credential_ceiling_actions"] = "range.manage\nrange.manage"
    with pytest.raises(ValueError, match="Operation authority unavailable"):
        credential_reference_from_record(SimpleNamespace(**fields))
