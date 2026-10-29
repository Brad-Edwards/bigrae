"""Durable admission carries registered non-secret references and exact ceilings."""

from types import SimpleNamespace
from uuid import uuid4

import pytest

from shared.authorization import CredentialCeiling, TargetRef
from shared.credentials import CredentialContext
from shared.identity_scope import PrincipalRef, ResourceScope
from shared.operation_authority import (
    authority_resource_scope,
    credential_reference_fields,
    credential_reference_from_fields,
    credential_reference_from_record,
)


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
    assert credential_reference_from_fields(fields) == credential
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
    with pytest.raises(ValueError, match="Operation authority unavailable"):
        credential_reference_from_fields(fields)


@pytest.mark.parametrize("change", ["missing", "text-uuid", "untyped-ceiling", "invalid-kind"])
def test_scalar_snapshot_decoder_requires_the_same_closed_record_shape(change):
    credential = CredentialContext(
        PrincipalRef(uuid4(), "human"), "session", uuid4(), CredentialCeiling(frozenset({"range.read"})), frozenset()
    )
    fields = credential_reference_fields(credential)
    if change == "missing":
        fields.pop("credential_uuid")
    elif change == "text-uuid":
        fields["credential_uuid"] = str(credential.credential_uuid)
    elif change == "untyped-ceiling":
        fields["credential_ceiling_actions"] = None
    else:
        fields["credential_kind"] = "unknown"
    with pytest.raises(ValueError, match="Operation authority unavailable"):
        credential_reference_from_fields(fields)


def test_recorded_authority_scope_preserves_resolved_ancestry():
    account, organization, workspace = uuid4(), uuid4(), uuid4()
    assert authority_resource_scope(
        {
            "authority_scope_kind": "account",
            "authority_account_uuid": account,
            "authority_organization_uuid": organization,
            "authority_workspace_uuid": workspace,
        }
    ) == ResourceScope("account", account, organization, workspace)


@pytest.mark.parametrize("change", ["missing-account", "text-identity", "invalid-kind", "installation-with-account"])
def test_recorded_authority_scope_rejects_incomplete_or_conflicting_identity(change):
    fields = {
        "authority_scope_kind": "account",
        "authority_account_uuid": uuid4(),
        "authority_organization_uuid": None,
        "authority_workspace_uuid": None,
    }
    if change == "missing-account":
        fields["authority_account_uuid"] = None
    elif change == "text-identity":
        fields["authority_account_uuid"] = str(uuid4())
    else:
        fields["authority_scope_kind"] = "installation" if change == "installation-with-account" else "unknown"
    with pytest.raises(ValueError):
        authority_resource_scope(fields)
