"""Explicitly run contract conformance for an immutable registered pack."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from django.db import transaction

from shared.audit import AuditAction, AuditEntityType, AuditEvent, audit_log, principal_actor_fields
from shared.credentials import CredentialContext, resolve_local_credential
from shared.exceptions import ValidationError
from shared.raes.pack_conformance import validate_pack_contract

if TYPE_CHECKING:
    from django.contrib.auth.models import User

    from cms.models import RaesPackageSource
    from shared.identity_scope import ResourceScope


def validate_registered_pack_conformance(
    *, user: User | CredentialContext, scenario_id: str, expected_package_digest: str
) -> str:
    """Verify current private bytes, compile through the actual backend, then CAS.

    Callers supply identity and request validation; they cannot supply a result
    or a trusted report. The same check is usable after tenant setup for object
    packs. It creates no range, availability record, adapter or worker authority.
    """
    from cms.models import RaesPackageSource
    from cms.scenarios.realizability import _trusted_scenario_path
    from cms.scenarios.registry import get_catalog_entry

    from ._content_authority import (
        content_scope,
        fence_content_scope,
        require_content_scope,
        require_native_content_credential,
    )

    user = require_native_content_credential(user)
    source = RaesPackageSource.objects.filter(scenario_id=scenario_id).first()
    scope = content_scope(source)
    user = require_content_scope(user, scope, "manage_content")
    if get_catalog_entry(scenario_id, user=user, scope=scope) is None:
        raise ValidationError("The pack is unavailable for conformance validation")
    if source is None or not expected_package_digest or source.package_digest != expected_package_digest:
        raise ValidationError("The pack identity changed before conformance validation")
    identity = _identity(source)
    report = "urn:shifter:pack-conformance:" + str(uuid4())
    try:
        with _trusted_scenario_path(source) as (scenario_path, _):
            if scenario_path is None:
                raise ValueError("pack integrity verification failed")
            validate_pack_contract(scenario_path, report)
    except Exception as exc:
        # Private parser diagnostics and storage references never leave this gate.
        raise ValidationError("The registered pack failed contract conformance") from exc
    user = require_content_scope(user, scope, "manage_content")
    with transaction.atomic():
        fence_content_scope(user, scope)
        current = _locked_unchanged(source.pk, identity)
        _fence_conformance_source(user, scope, current, source)
        current.conformance_status = "passed"
        current.conformance_report_ref = report
        current.save(update_fields=["conformance_status", "conformance_report_ref", "updated_at"])
        audit_log(
            AuditEvent(
                entity_type=AuditEntityType.SCENARIO,
                entity_id=0,
                entity_ref=scenario_id,
                action=AuditAction.UPDATE,
                **principal_actor_fields(user.principal),
                new_state={
                    "scenario_id": scenario_id,
                    "package_digest": expected_package_digest,
                    "conformance_status": "passed",
                    "conformance_report_ref": report,
                },
            ),
            strict=True,
        )
    return "passed"


def _identity(source: RaesPackageSource) -> tuple[str, ...]:
    """Handle identity."""
    return tuple(
        getattr(source, key)
        for key in (
            "source_kind",
            "contract_kind",
            "contract_profile",
            "package_ref",
            "package_version",
            "package_digest",
            "lock_ref",
            "lock_digest",
        )
    )


def _locked_unchanged(pk: UUID, identity: tuple[str, ...]) -> RaesPackageSource:
    """Lock the validated row, reauthorize, and require its identity unchanged (or present)."""
    from cms.models import RaesPackageSource

    current = RaesPackageSource.objects.select_for_update().filter(pk=pk).first()
    if current is None:
        raise ValidationError("The pack identity changed during conformance validation")
    if _identity(current) != identity:
        raise ValidationError("The pack identity changed during conformance validation")
    return current


def _fence_conformance_source(
    user: CredentialContext, scope: ResourceScope, current: RaesPackageSource, source: RaesPackageSource
) -> None:
    """Reject revoked proof or replaced native source custody at qualification commit."""
    from ._content_authority import content_scope

    if (
        resolve_local_credential(user) != user
        or content_scope(current) != scope
        or current.owner_principal_uuid != source.owner_principal_uuid
        or current.scope_generation != source.scope_generation
    ):
        raise ValidationError("Native pack conformance authority unavailable")
