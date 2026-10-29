"""Engine-side creation and dispatch for the canonical RAES path (ADR-031).

The persisted truth is the serialized RAES ``ProvisioningPlan`` stored in
``mission_control_range.range_config`` and realized by the provisioner's
``raes-range`` command. This module is reached through the RAES dispatch port
(``cms.raes.dispatch``) and is the only range-creation authority.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone
from installation.range_egress import RangeEgressMode

from engine.ecs import start_raes_range_provisioning
from shared.enums import RequestType

from ._common import _persist_task_arn
from ._raes_range_bindings import (
    RangeBindings as RangeBindings,
)
from ._raes_range_bindings import (
    _persist_range_bindings as _persist_range_bindings,
)
from ._raes_range_bindings import (
    _verify_existing_participant_access as _verify_existing_participant_access,
)
from ._raes_range_bindings import (
    _verify_existing_plugin_scope as _verify_existing_plugin_scope,
)
from ._raes_range_bindings import (
    _verify_existing_remote_access as _verify_existing_remote_access,
)
from ._raes_range_bindings import (
    grant_raes_remote_access as grant_raes_remote_access,
)
from ._raes_range_bindings import (
    mint_remote_access_capability as mint_remote_access_capability,
)
from ._range_backend_binding import (
    assert_backend_supports_egress,
    backend_binding_fields,
    egress_binding_fields,
    require_workspace_binding,
    verify_existing_binding,
    verify_existing_egress_binding,
    verify_existing_workspace_binding,
)
from ._range_placement import select_placement_zone
from ._runtime_plugin_bindings import (
    persist_runtime_plugin_pin,
    resolve_runtime_plugin_pin,
)

if TYPE_CHECKING:
    from uuid import UUID

    from engine.models import Range
    from shared.credentials import CredentialContext
    from shared.range_instantiation_policy import BackendAdmission

    from ._principal_range_create import PreparedRaesRangeCreation


__all__ = [
    "RaesRangeRef",
    "RangeBindings",
    "create_raes_range",
    "dispatch_created_raes_range",
    "grant_raes_remote_access",
    "mint_remote_access_capability",
]


@dataclass(frozen=True)
class RaesRangeRef:
    """IDs/status returned to the dispatch port after an RAES range is accepted."""

    request_id: str
    range_id: str
    status: str
    accepted: bool


def create_raes_range(
    *,
    request_id: str | UUID,
    user_id: int | None,
    compiled_plan: dict[str, Any],
    workspace_id: int | None,
    egress_mode: str = RangeEgressMode.STATUS_QUO.value,
    backend_admission: BackendAdmission | None = None,
    bindings: RangeBindings | None = None,
) -> RaesRangeRef:
    """Create + dispatch an RAES-native range from a serialized RAES plan.

    Persists an ``engine.models.Request`` and ``engine.models.Range`` (with
    ``range_config`` = the serialized RAES ProvisioningPlan, a plain dict that is
    self-describing via its ``kind``/``raes_version`` -- no cyberscript
    envelope and no Shifter-owned spec, ADR-031-R1 / ADR-032) keyed by
    ``request_id``, writes an ``operation_receipt`` sidecar, and dispatches the
    provisioner ``raes-range provision`` task. Idempotent on ``request_id``. On a
    dispatch failure the range is marked FAILED and the error re-raised, so a
    dispatch failure is never silent (mirrors ``create_range``).

    ``backend_admission`` is the trusted #1348 admission result carried beside the
    RAES plan (never inside it, ADR-031-R1/R2). The normalized (backend, purpose)
    is persisted as the write-once #1666 ownership binding in the same transaction
    as the Range, before dispatch; ``None`` on non-GCP providers.

    ``workspace_id`` is the trusted #1325 tenancy scope resolved and authorized by
    the CMS launch facade and carried the same way. It is **required** and
    persisted in the same transaction, so a range is never visible -- briefly or
    durably -- without its scope (ADR-046-R3).

    ``bindings`` groups the byte-free sidecar identities that ride beside the plan
    (see :class:`RangeBindings`); each collection is persisted in the same
    transaction as the Range. On the idempotent existing-range reuse path bindings
    are not re-created -- the first create already persisted them.

    ``bindings.delivery`` are the #1564 ``DeliveryBinding`` identities, each
    persisted as one ``engine.models.RaesContentDeliveryBinding`` row.

    ``bindings.participant_access`` are the #1710 non-secret
    ``ParticipantAccessBinding`` declarations, persisted as
    ``engine.models.RaesParticipantAccessBinding`` rows (ADR-032-R10). Because they
    are the immutable declaration the realized access is later compared against, an
    idempotent replay of the same ``request_id`` carrying *different* access intent
    is rejected rather than silently reusing the first declaration.

    ``bindings.artifact`` are the #1580 generation-fenced ``ArtifactBinding``
    decisions -- each authored artifact requirement the CMS launch resolved to a
    concrete backend image -- persisted as ``engine.models.RaesArtifactSatisfactionBinding``
    rows (ADR-034-R8). The provisioner realizes exactly these and never
    re-resolves; on the idempotent reuse path they are not re-created.
    """
    bindings = bindings or RangeBindings()
    # Imported lazily so importing the ``engine`` app does not define models
    # before the app registry is ready.
    from engine.models import Range, Request

    from ._principal_range_create import (
        assert_principal_range_creation,
        verify_principal_range_replay,
    )

    principal_fields = _prepared_range_creation_fields(bindings, workspace_id, user_id, compiled_plan)
    request_uuid = request_id if isinstance(request_id, UUID) else UUID(str(request_id))

    existing = Range.objects.filter(request__request_id=request_uuid).first()
    if existing is not None:
        verify_principal_range_replay(existing, principal_fields, bindings, compiled_plan)
        verify_existing_binding(existing, request_uuid, backend_admission)
        verify_existing_workspace_binding(existing, request_uuid, workspace_id)
        verify_existing_egress_binding(existing, request_uuid, egress_mode)
        _verify_existing_participant_access(existing, bindings.participant_access)
        _verify_existing_plugin_scope(existing, bindings.runtime_plugin_scope)
        _verify_existing_remote_access(existing, bindings.openvpn_deadline)
        return RaesRangeRef(
            request_id=str(request_uuid), range_id=str(existing.uuid), status=existing.status, accepted=True
        )

    binding_fields = backend_binding_fields(backend_admission)
    egress_fields = egress_binding_fields(egress_mode)
    assert_backend_supports_egress(binding_fields.get("range_backend"), egress_fields["egress_mode"])
    capability = mint_remote_access_capability(
        bindings.participant_access,
        bindings.openvpn_deadline,
        str(binding_fields.get("range_backend") or ""),
    )
    user_model = get_user_model()
    with transaction.atomic():
        _lock_native_warm_creation(request_uuid, bindings, bool(principal_fields))
        scope = bindings.runtime_plugin_scope
        pin = (
            resolve_runtime_plugin_pin(scope, compiled_plan, backend=str(binding_fields.get("range_backend") or ""))
            if scope
            else None
        )
        if principal_fields:
            assert_principal_range_creation(bindings, principal_fields)
        user = user_model.objects.get(id=user_id) if user_id is not None else None
        request = Request.objects.create(
            request_id=request_uuid,
            request_type=RequestType.RANGE.value,
            user=user,
            **(
                {
                    "owner_principal_uuid": principal_fields["owner_principal_uuid"],
                    "scope_kind": principal_fields["scope_kind"],
                    "account_id": principal_fields["account_id"],
                    "organization_id": principal_fields["organization_id"],
                    "workspace_id": workspace_id,
                }
                if principal_fields
                else {}
            ),
        )
        subnet_index = Range.allocate_subnet_index()
        # #2029 realized multi-region placement: pick the zone from the
        # RANGE_NETWORK_ZONES pool in the same transaction as the slot (empty keeps
        # single-zone). The provisioner reads it back and never recomputes.
        placement_zone = select_placement_zone(subnet_index - 1)
        range_obj = Range.objects.create(
            uuid=uuid4(),
            user=user,
            request=request,
            cms_user_id=user_id,
            status=Range.Status.PROVISIONING,
            subnet_index=subnet_index,
            placement_zone=placement_zone,
            resource_generation=_new_resource_generation(bool(principal_fields), binding_fields.get("range_backend")),
            range_config=compiled_plan,
            workspace_id=workspace_id,
            remote_access_capability=capability,
            **binding_fields,
            **egress_fields,
            **principal_fields,
        )
        _persist_range_bindings(range_obj, bindings)
        persist_runtime_plugin_pin(range_obj, pin)
        _write_operation_receipt(request_uuid, range_id=str(range_obj.uuid))
        _record_creation_authority(range_obj, bindings, bool(principal_fields))

    return _dispatch_range_creation(range_obj, request_uuid, bindings)


def dispatch_created_raes_range(
    request_uuid: UUID,
    *,
    credential: CredentialContext | None = None,
    prepared_creation: PreparedRaesRangeCreation | None = None,
) -> RaesRangeRef:
    """Dispatch an already persisted range through the incumbent launch outbox."""
    from engine.models import Range

    range_obj = Range.objects.get(request__request_id=request_uuid)
    if range_obj.owner_principal_uuid is not None or credential is not None:
        from ._principal_range_create import dispatch_principal_raes_range

        if credential is None:
            raise PermissionError("Range authority unavailable")
        return dispatch_principal_raes_range(range_obj, credential, prepared_creation)
    try:
        task_ref = start_raes_range_provisioning(request_uuid)
    except Exception:
        range_obj.status = Range.Status.FAILED
        range_obj.error_message = "Provisioning dispatch failed"
        range_obj.save(update_fields=["status", "error_message", "updated_at"])
        from ._receipt import revoke_receipt_verifier

        revoke_receipt_verifier(request_uuid)
        raise
    if task_ref:
        _persist_task_arn(range_obj, "provision", task_ref)

    return RaesRangeRef(
        request_id=str(request_uuid), range_id=str(range_obj.uuid), status=range_obj.status, accepted=True
    )


def _write_operation_receipt(request_id: UUID, *, range_id: str) -> None:
    """Write the idempotent operation_receipt sidecar for an accepted provision."""
    # Lazy import: shared.raes.operations pulls shared.models, which must not load
    # during app population (mirrors _raes_status.py).
    from shared.raes.operations import persist_operation_receipt_record

    operation_id = str(request_id)
    persist_operation_receipt_record(
        request_id=request_id,
        operation_id=operation_id,
        source_timestamp=timezone.now(),
        payload={"operation_id": operation_id, "accepted": True, "status": "accepted"},
        range_id=range_id,
    )


def _prepared_range_creation_fields(
    bindings: RangeBindings, workspace_id: int | None, user_id: int | None, compiled_plan: dict[str, Any]
) -> dict[str, object]:
    """Choose the already-admitted principal custody or the incumbent workspace binding before creation."""
    from ._principal_range_create import prepare_principal_range_fields, prepared_creation_fields

    principal_fields: dict[str, object] = {}
    if bindings.credential is not None:
        principal_fields = (
            prepared_creation_fields(bindings.prepared_creation, bindings, workspace_id, user_id, compiled_plan)
            if bindings.prepared_creation is not None
            else prepare_principal_range_fields(bindings, workspace_id, user_id)
        )
    else:
        if bindings.resource_scope is not None or bindings.cms_range_uuid is not None:
            raise ValueError("Range creation authority unavailable")
        require_workspace_binding(workspace_id)
    return principal_fields


def _dispatch_range_creation(range_obj: Range, request_uuid: UUID, bindings: RangeBindings) -> RaesRangeRef:
    """Honor the enclosing CMS transaction before dispatching one immutable native range generation."""
    if bindings.defer_dispatch:
        return RaesRangeRef(str(request_uuid), str(range_obj.uuid), range_obj.status, True)
    if bindings.credential is not None:
        return dispatch_created_raes_range(
            request_uuid,
            credential=bindings.credential,
            prepared_creation=bindings.prepared_creation,
        )
    return dispatch_created_raes_range(request_uuid)


def _new_resource_generation(principal_owned: bool, range_backend: object) -> UUID | None:
    """Mint native custody generations for principal ranges and EC2 execution."""
    return uuid4() if principal_owned or range_backend == "ec2" else None


def _lock_native_warm_creation(request_uuid: UUID, bindings: RangeBindings, principal_owned: bool) -> None:
    """Acquire the existing installation warm ledger fence before native range creation."""
    if principal_owned and bindings.resource_scope is not None and bindings.resource_scope.kind == "installation":
        from ._range_authority_history import lock_warm_range_creation

        lock_warm_range_creation(request_uuid)


def _record_creation_authority(range_obj: Range, bindings: RangeBindings, principal_owned: bool) -> None:
    """Retain exact native creation authority after its receipt in the same SQL transaction."""
    if principal_owned:
        from ._range_authority_history import record_range_authority_change

        if bindings.credential is None:
            raise ValueError("Range creation authority unavailable")
        record_range_authority_change(range_obj, bindings.credential, "create", "")
