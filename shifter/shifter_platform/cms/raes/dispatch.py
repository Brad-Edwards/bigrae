"""CMS-side concrete RAES provisioning dispatch port (ADR-031, ADR-032, ADR-024).

Concrete implementation of
:class:`shared.raes.dispatch_port.ShifterProvisioningDispatchPort`. It hands the
serialized RAES ``ProvisioningPlan`` to the engine service seam
(:func:`engine.services.create_raes_range`), which persists it keyed by
``request_id``, writes the operation-receipt sidecar, and dispatches the
provisioner ``raes-range`` task.

Conformance boundary (ADR-031-R1, ADR-032): the RAES code path imports **no**
cyberscript module and carries **no** cyberscript semantics. The persisted
artifact is the serialized RAES plan itself (a plain dict, self-describing via
its ``kind``/``raes_version``) -- not a cyberscript envelope and not a
Shifter-owned spec. This module imports only ``shared.raes`` and the public
``engine.services`` facade.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from django.conf import settings
from django.db import transaction
from installation.range_egress import RangeEgressMode

from engine.services import RangeBindings, create_raes_range
from shared.raes.dispatch_port import ShifterDispatchResult

if TYPE_CHECKING:
    from datetime import datetime
    from pathlib import Path

    from cms.models import RaesPackageSource, RangeInstance
    from engine.services import PreparedRaesRangeCreation, RaesRangeRef
    from shared.authorization import TargetRef
    from shared.credentials import CredentialContext
    from shared.identity_scope import PrincipalRef, ResourceScope
    from shared.raes.artifact_binding import ArtifactBinding
    from shared.raes.artifact_inventory import ArtifactRequirements, ArtifactSupply, BackendArtifact
    from shared.raes.content_delivery import DeliveryBinding
    from shared.raes.participant_access import ParticipantAccessBinding
    from shared.range_instantiation_policy import BackendAdmission
    from shared.runtime_plugin_binding import RuntimePluginScope
    from workspaces.services import ResolvedResourceScope

__all__ = ["CmsRaesDispatchPort"]

# Realization backends whose tenant image registry can own portable artifacts
# (the RAES-native GCE adapter). A backend outside this set has no artifact
# inventory, so resolution is skipped rather than querying an unknown provider.
_ARTIFACT_REGISTRY_PROVIDERS = frozenset({"gce"})


@dataclass(frozen=True)
class CmsRaesDispatchPort:
    """Realize a serialized RAES plan through the engine RAES range service.

    Constructed per launch with the CMS launch context (the owning user id and
    the Shifter ``request_id`` the operation is keyed by). The RuntimeTarget
    backend validates + serializes the plan and calls :meth:`realize`.

    ``backend_admission`` is the trusted #1348 admission result captured at the
    CMS live-fire gate and carried beside the RAES plan (never inside it, per
    ADR-031-R1/R2). The engine binds the immutable #1666 (backend, purpose)
    ownership fields from it at create; ``None`` on non-GCP providers.

    ``pack_root`` is the live, digest-verified pack directory for this launch. It
    is the one point where the pack bytes and the compiled plan coexist (#1564),
    so :meth:`realize` prepares source-backed content delivery here: it
    materializes each source-backed content item, promotes it content-addressed to
    object storage, and hands the engine the byte-free delivery bindings to persist
    beside the plan (ADR-032-R3). ``None`` only in tests / plans that carry no
    source-backed content.

    ``workspace_id`` is the trusted #1325 tenancy scope resolved and authorized by
    the CMS launch facade. It rides beside the plan for the same reason
    ``backend_admission`` does: Engine persists it, and never resolves or
    authorizes a workspace itself (ADR-046-R1/R3).
    """

    user_id: int | None
    request_id: str
    # Required: the port cannot realize a range without a tenancy scope, and a
    # nullable field here would just push the failure into the Engine boundary.
    workspace_id: int | None
    backend_admission: BackendAdmission | None = None
    pack_root: Path | None = None
    # Effective range egress posture resolved under the workspace mutex by the CMS
    # launch-admission seam (PLAT-238, ADR-017-R5). Rides beside the plan like
    # ``workspace_id``; the Engine pins it on the range at create. Defaults to the
    # compatibility ``status-quo`` for constructors that predate the field.
    egress_mode: str = RangeEgressMode.STATUS_QUO.value
    runtime_plugin_scope: RuntimePluginScope | None = None
    credential: CredentialContext | None = None
    scope: ResourceScope | None = None
    source: RaesPackageSource | None = None
    owner_principal: PrincipalRef | None = None
    authority_action: str | None = None
    authority_target: TargetRef | None = None

    def realize(
        self,
        compiled_plan: dict[str, Any],
        participant_access: Sequence[ParticipantAccessBinding] = (),
    ) -> ShifterDispatchResult:
        if self.credential is not None:
            return self._realize_principal(compiled_plan, participant_access)
        delivery_bindings = self._prepare_delivery(compiled_plan)
        artifact_bindings = self._resolve_artifact_bindings(compiled_plan)
        from uuid import UUID

        from cms.services._model_allocation import needs_model_preparation, prepare_model_access_for_dispatch
        from engine.services import dispatch_created_raes_range

        prepare_models = needs_model_preparation(self.request_id)
        with transaction.atomic():
            ref = create_raes_range(
                request_id=self.request_id,
                user_id=self.user_id,
                compiled_plan=compiled_plan,
                backend_admission=self.backend_admission,
                bindings=RangeBindings(
                    delivery=delivery_bindings,
                    participant_access=tuple(participant_access),
                    artifact=artifact_bindings,
                    runtime_plugin_scope=self.runtime_plugin_scope,
                    defer_dispatch=prepare_models,
                    openvpn_deadline=self._openvpn_deadline(),
                ),
                workspace_id=self.workspace_id,
                egress_mode=self.egress_mode,
            )
        if prepare_models:
            prepare_model_access_for_dispatch(UUID(self.request_id), range_id=UUID(ref.range_id))
            ref = dispatch_created_raes_range(UUID(self.request_id))
        return ShifterDispatchResult(
            request_id=ref.request_id, accepted=ref.accepted, status=ref.status, range_id=ref.range_id
        )

    def _realize_principal(
        self, compiled_plan: dict[str, Any], participant_access: Sequence[ParticipantAccessBinding]
    ) -> ShifterDispatchResult:
        """Stage policy before locks; native creation, source fences and queue commit together."""
        from uuid import UUID

        from cms.models import RangeInstance
        from cms.services._model_allocation import needs_model_preparation, prepare_model_access_for_dispatch
        from cms.services._scoped_range_launch import revalidate_scoped_launch, source_launch_metadata
        from engine.services import dispatch_created_raes_range, prepare_raes_range_creation

        if self.credential is None or self.scope is None or self.source is None:
            raise ValueError("Range creation authority unavailable")
        current = revalidate_scoped_launch(
            self.credential,
            self.scope,
            self.source,
            authority_action=self.authority_action,
            authority_target=self.authority_target,
        )
        delivery = self._prepare_delivery(compiled_plan)
        artifacts = self._resolve_artifact_bindings(compiled_plan)
        current = revalidate_scoped_launch(
            current,
            self.scope,
            self.source,
            authority_action=self.authority_action,
            authority_target=self.authority_target,
        )
        metadata = source_launch_metadata(self.source.scenario_id)
        if not metadata[0]:
            raise ValueError("Range source unavailable")
        instance = RangeInstance.objects.select_related("request").get(request__request_id=self.request_id)
        prepare_models = needs_model_preparation(self.request_id)
        bindings = RangeBindings(
            delivery=delivery,
            participant_access=tuple(participant_access),
            artifact=artifacts,
            runtime_plugin_scope=self.runtime_plugin_scope,
            defer_dispatch=prepare_models or self.authority_target is not None,
            openvpn_deadline=self._openvpn_deadline(),
            credential=current,
            resource_scope=self.scope,
            cms_range_uuid=instance.uuid,
            owner_principal=self.owner_principal,
            authority_action=self.authority_action,
            authority_target=self.authority_target,
        )
        prepared = prepare_raes_range_creation(bindings, self.workspace_id, instance.user_id, compiled_plan)
        ref = self._commit_principal_runtime(instance, current, metadata, bindings, prepared, compiled_plan)
        if prepare_models:
            prepare_model_access_for_dispatch(UUID(self.request_id), range_id=UUID(ref.range_id))
            if self.authority_target is None:
                ref = dispatch_created_raes_range(UUID(self.request_id), credential=current, prepared_creation=prepared)
        return ShifterDispatchResult(
            request_id=ref.request_id, accepted=ref.accepted, status=ref.status, range_id=ref.range_id
        )

    def _commit_principal_runtime(
        self,
        instance: RangeInstance,
        current: CredentialContext,
        metadata: tuple[bool, bool],
        bindings: RangeBindings,
        prepared: PreparedRaesRangeCreation,
        compiled_plan: dict[str, Any],
    ) -> ShifterDispatchResult | RaesRangeRef:
        """Commit the prepared exact runtime after the event and source fences acquire locks."""
        from dataclasses import replace

        from cms.models import RangeInstance

        with transaction.atomic():
            self._fence_event_launch(instance)
            row = RangeInstance.objects.select_for_update(of=("self",)).select_related("request").get(pk=instance.pk)
            self._assert_principal_launch_row(row, current, metadata)
            ref: ShifterDispatchResult | RaesRangeRef | None = self._replay_runtime(row)
            if ref is None:
                ref = create_raes_range(
                    request_id=self.request_id,
                    user_id=row.user_id,
                    compiled_plan=compiled_plan,
                    workspace_id=self.workspace_id,
                    backend_admission=self.backend_admission,
                    egress_mode=self.egress_mode,
                    bindings=replace(bindings, prepared_creation=prepared),
                )
            native_id = self._native_runtime_id()
            if row.range_id is not None and row.range_id != native_id:
                raise ValueError("Range runtime changed")
            row.range_id = native_id
            row.save(update_fields=["range_id"])
        return ref

    def _fence_event_launch(self, instance: RangeInstance) -> None:
        """Serialize a retained event launch with cancellation before locking its CMS row."""
        from uuid import UUID

        if instance.launch_intent_digest:
            from shared.range_workspace_aggregate import fence_range_event_launch

            if (
                self.authority_target is None
                or self.authority_target.type != "event"
                or self.authority_target.uuid is None
            ):
                raise ValueError("Range launch intent unavailable")
            fence_range_event_launch(UUID(self.request_id), self.authority_target.uuid)

    def _replay_runtime(self, row: RangeInstance) -> ShifterDispatchResult | None:
        """An identical retained event runtime is reused without another native create."""
        from uuid import UUID

        if not row.launch_intent_digest or self.authority_target is None or row.range_id is None:
            return None

        from engine.services import project_range_authority

        projection = project_range_authority(UUID(self.request_id))
        if (
            projection.native_id,
            projection.public_uuid,
            projection.scope,
            projection.owner_principal_uuid,
            projection.parent,
        ) != (row.range_id, row.uuid, self.scope, row.owner_principal_uuid, self.authority_target):
            raise ValueError("Range replay runtime changed")
        return ShifterDispatchResult(
            request_id=self.request_id, accepted=True, status=row.status, range_id=str(projection.native_uuid)
        )

    def _native_runtime_id(self) -> int:
        """Resolve the accepted runtime in its installation or customer authority domain."""
        from uuid import UUID

        if self.scope is None:
            raise ValueError("Range creation authority unavailable")

        if self.scope.kind == "installation":
            from engine.services import native_range_id_by_request

            return native_range_id_by_request(UUID(self.request_id))
        else:
            from engine.services import project_range_authority

            return project_range_authority(UUID(self.request_id)).native_id

    def _assert_principal_launch_row(
        self, row: RangeInstance, current: CredentialContext, metadata: tuple[bool, bool]
    ) -> None:
        """Fence locked native custody and the original source before dispatch."""
        from cms.services._pack_fence import fence_pack_reference
        from workspaces.services import resolve_resource_scope

        if self.scope is None or self.source is None or row.request is None:
            raise ValueError("Range creation authority unavailable")
        if row.launch_intent_digest:
            from shared.operation_authority import credential_reference_fields

            if any(getattr(row.request, key) != value for key, value in credential_reference_fields(current).items()):
                raise ValueError("Range original proof changed")
        source = fence_pack_reference(self.source.scenario_id, expected_digest=self.source.package_digest)
        resolved = resolve_resource_scope(self.scope)
        self._assert_principal_custody(row, current, resolved)
        self._assert_launch_source(source, metadata)

    def _assert_launch_source(self, source: RaesPackageSource | None, metadata: tuple[bool, bool]) -> None:
        """Require the exact registered source and availability inspected before staging."""
        from cms.services._scoped_launch_source import source_launch_binding, source_launch_metadata

        if self.source is None:
            raise ValueError("Range creation authority unavailable")
        if (
            source is None
            or source_launch_binding(source) != source_launch_binding(self.source)
            or source_launch_metadata(self.source.scenario_id) != metadata
            or not metadata[0]
        ):
            raise ValueError("Range launch changed")

    def _assert_principal_custody(
        self, row: RangeInstance, current: CredentialContext, resolved: ResolvedResourceScope
    ) -> None:
        """Require both CMS custody records, current native proof and exact resolved ancestry."""
        from shared.credentials import resolve_local_credential

        if row.request is None:
            raise ValueError("Range creation authority unavailable")
        if (
            resolve_local_credential(current) != current
            or row.status != "provisioning"
            or row.owner_principal_uuid
            != (self.owner_principal.uuid if self.owner_principal is not None else current.principal.uuid)
            or (row.scope_kind, row.account_id, row.organization_id, row.workspace_id, row.owner_principal_uuid)
            != (
                row.request.scope_kind,
                row.request.account_id,
                row.request.organization_id,
                row.request.workspace_id,
                row.request.owner_principal_uuid,
            )
            or (row.scope_kind, row.account_id, row.organization_id, row.workspace_id)
            != (resolved.kind, resolved.account_id, resolved.organization_id, resolved.workspace_id)
        ):
            raise ValueError("Range launch changed")

    def _openvpn_deadline(self) -> datetime | None:
        """Return this launch's OpenVPN deadline: its persisted lease ceiling, when admitted (#2030)."""
        from cms.models import RangeInstance
        from cms.services._range_remote_access import openvpn_deadline

        admission = self.backend_admission
        lease_ceiling = (
            RangeInstance.objects.filter(request__request_id=self.request_id)
            .values_list("maximum_expires_at", flat=True)
            .first()
        )
        return openvpn_deadline(admission.backend if admission and admission.admitted else None, lease_ceiling)

    def _resolve_artifact_bindings(self, compiled_plan: dict[str, Any]) -> tuple[ArtifactBinding, ...]:
        """Resolve authored artifact requirements to fenced bindings at launch (#1580).

        ALWAYS runs the resolver over the plan's authored requirements -- it never
        short-circuits on the backend before inspecting requirements. A plan with no
        artifact requirement resolves to no bindings; a plan carrying a requirement
        the selected backend cannot satisfy (including a backend with no artifact
        registry, whose inventory is empty) fails closed: ``resolve_plan_artifact_bindings``
        raises, the dispatch is not accepted, and the range reservation is marked
        FAILED rather than a missing binding letting the provisioner fall through to
        legacy resolution. An exact requirement is never substituted (ADR-034-R8;
        codex #1580 review).
        """
        from shared.raes.artifact_inventory import resolve_plan_artifact_bindings
        from shared.raes.manifest import shifter_artifact_mechanism_capabilities, shifter_backend_apparatus

        return resolve_plan_artifact_bindings(
            compiled_plan,
            inventory=self._backend_inventory(),
            capabilities=shifter_artifact_mechanism_capabilities(),
            backend=shifter_backend_apparatus(),
        )

    def artifact_supply(self, requirements: ArtifactRequirements) -> ArtifactSupply:
        """Give planning the same admitted backend inventory used to fence dispatch."""
        from shared.raes.artifact_inventory import build_artifact_supply

        return build_artifact_supply(requirements, self._backend_inventory())

    def _backend_inventory(self) -> tuple[BackendArtifact, ...]:
        """Return the selected backend's owned artifact inventory, or empty when it has none.

        A backend outside the artifact-registry set (or an absent admission) owns no
        portable artifacts, so its inventory is empty and any authored requirement
        resolves unsatisfiable -- the resolver then fails the launch closed rather
        than skipping resolution.
        """
        provider = self.backend_admission.backend if self.backend_admission is not None else None
        if provider not in _ARTIFACT_REGISTRY_PROVIDERS:
            return ()
        from engine.services import list_backend_artifacts

        if self.credential is not None:
            return tuple(list_backend_artifacts(provider=provider, credential=self.credential, scope=self.scope))
        return tuple(list_backend_artifacts(provider=provider))

    def _prepare_delivery(self, compiled_plan: dict[str, Any]) -> tuple[DeliveryBinding, ...]:
        """Materialize + promote source-backed content, returning byte-free bindings.

        Returns an empty tuple for the common case of a plan with no source-backed
        content (``prepare_content_delivery`` short-circuits before touching the
        pack or object storage). Any preparation failure raises
        ``ContentDeliveryError``, which the RuntimeTarget apply boundary turns into
        a non-accepted dispatch so the range reservation is marked FAILED.
        """
        from shared.raes.content_delivery_prep import (
            DeliveryTarget,
            has_source_backed_content,
            prepare_content_delivery,
        )

        # Cheap precheck: skip object-storage / pack resolution entirely for the
        # common plan with no source-backed content.
        if not has_source_backed_content(compiled_plan):
            return ()

        from shared.cloud import get_object_storage

        target = DeliveryTarget(
            storage=get_object_storage(),
            bucket=settings.STORAGE_BUCKET_NAME,
            prefix=settings.RAES_CONTENT_DELIVERY_PREFIX,
        )
        from cms.raes.feature_artifacts import feature_artifact_resolver

        return prepare_content_delivery(
            pack_root=self.pack_root,
            serialized_plan=compiled_plan,
            target=target,
            acquire_feature=feature_artifact_resolver(compiled_plan),
        )
