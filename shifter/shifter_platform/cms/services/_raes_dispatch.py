"""RAES pack resolution and dispatch helpers for the native launch path.

Split from ``_raes_range_create`` (python:S104 file-size gate): the repo/object
pack resolvers, the object-key joiner, and the canonical launch tail live here,
while ``_raes_range_create._dispatch_raes_package`` routes to them by source kind.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from django.conf import settings
from django.contrib.auth.models import User

from cms.exceptions import CMSError
from shared.credentials import CredentialContext
from shared.log_sanitize import safe_log_fingerprint, safe_log_value

if TYPE_CHECKING:
    from uuid import UUID

    from django.contrib.auth.models import User

    from cms.models import RaesPackageSource
    from shared.authorization import TargetRef
    from shared.credentials import CredentialContext
    from shared.identity_scope import PrincipalRef, ResourceScope
    from shared.range_instantiation_policy import BackendAdmission
    from shared.runtime_plugin_binding import RuntimePluginScope


logger = logging.getLogger(__name__)

_OBJECT_SOURCE_KIND = "object"


_RANGE_AUTHORITY_UNAVAILABLE = "Range authority unavailable"


@dataclass(frozen=True)
class RaesDispatchOptions:
    """Installed backend, tenancy and admitted content context for one pack launch."""

    backend_admission: BackendAdmission | None
    workspace_id: int | None
    egress_mode: str
    content_authorizer: User | None = None
    principal_scope: ResourceScope | None = None
    owner_principal: PrincipalRef | None = None
    authority_action: str | None = None
    authority_target: TargetRef | None = None


def dispatch_repo_raes_package(
    request_id: UUID,
    user: User | CredentialContext,
    source: RaesPackageSource,
    options: RaesDispatchOptions,
) -> None:
    """Resolve a repo pack under ``RAES_PACKAGE_ROOT``, verify its digest, launch."""
    from cms.scenarios.pack_validation import PackDigestError, verify_pack_digest

    if options.principal_scope is not None:
        from ._scoped_range_launch import revalidate_scoped_launch

        if not isinstance(user, CredentialContext):
            raise CMSError(_RANGE_AUTHORITY_UNAVAILABLE)
        revalidate_scoped_launch(
            user,
            options.principal_scope,
            source,
            authority_action=options.authority_action,
            authority_target=options.authority_target,
        )
    from shared.raes.package_loader import RaesPackageError, resolve_pack_root

    try:
        pack_root = resolve_pack_root(source.package_ref, package_root=Path(settings.RAES_PACKAGE_ROOT))
    except RaesPackageError as exc:
        # RAES package errors embed server paths: log the class only and keep the text out of the chain.
        logger.error(
            "create_raes_native_range: repo package could not be resolved request_id=%s scenario_id=%s exc_type=%s",
            request_id,
            safe_log_value(source.scenario_id),
            type(exc).__name__,
            exc_info=False,
        )
        raise CMSError("RAES package could not be resolved") from None
    try:
        digest_matches = verify_pack_digest(pack_root, source.package_digest)
    except PackDigestError as exc:
        raise CMSError("RAES pack content identity could not be verified") from exc
    if not digest_matches:
        raise CMSError("RAES pack content digest no longer matches registration")
    _launch_pack(
        request_id,
        user,
        pack_root,
        options,
        _runtime_plugin_scope(
            user,
            options.workspace_id,
            source,
            options.content_authorizer,
            options.principal_scope,
            authority_action=options.authority_action,
            authority_target=options.authority_target,
        ),
        source=source if options.principal_scope is not None else None,
    )


def dispatch_object_raes_package(
    request_id: UUID,
    user: User | CredentialContext,
    source: RaesPackageSource,
    options: RaesDispatchOptions,
) -> None:
    """Stage an object-backed pack, bind its identity + digest, then launch.

    Object rows are registered without content validation or digest binding
    (#1578), so this resolver provides the equivalent identity guarantees repo
    packs get (ADR-034-R5): it downloads the single immutable archive named by
    ``package_ref`` into a private temp dir, safely extracts it, re-runs the
    upstream pack contract validation, asserts the pack identity matches the
    registered ``scenario_id``, and verifies the canonical ``package_digest`` --
    all before SDL resolution, planning, or dispatch. The staged directory is
    always cleaned up by the resolver context manager.
    """
    from cms.scenarios.pack_validation import (
        PackDigestError,
        PackValidationError,
        validate_pack,
        verify_pack_digest,
    )
    from shared.cloud import get_object_storage
    from shared.raes.object_source import stage_object_pack
    from shared.raes.package_loader import RaesPackageError

    if options.principal_scope is not None:
        from ._scoped_range_launch import revalidate_scoped_launch

        if not isinstance(user, CredentialContext):
            raise CMSError(_RANGE_AUTHORITY_UNAVAILABLE)
        revalidate_scoped_launch(
            user,
            options.principal_scope,
            source,
            authority_action=options.authority_action,
            authority_target=options.authority_target,
        )
    bucket = str(getattr(settings, "RAES_PACKAGE_BUCKET", "") or "").strip()
    if not bucket:
        raise CMSError("Object-backed RAES packages are not available: no package bucket is configured")

    try:
        with stage_object_pack(
            storage=get_object_storage(),
            bucket=bucket,
            key=_object_package_key(source.package_ref),
            max_archive_bytes=settings.RAES_PACKAGE_MAX_ARCHIVE_BYTES,
            max_uncompressed_bytes=settings.RAES_PACKAGE_MAX_UNCOMPRESSED_BYTES,
            max_entries=settings.RAES_PACKAGE_MAX_ENTRIES,
            expected_pack_name=source.package_identity,
        ) as pack_root:
            try:
                validated_name = validate_pack(pack_root)
            except PackValidationError as exc:
                raise CMSError("RAES pack failed validation") from exc
            if validated_name != source.package_identity:
                raise CMSError("RAES pack identity does not match the registered scenario")
            try:
                digest_matches = verify_pack_digest(pack_root, source.package_digest)
            except PackDigestError as exc:
                raise CMSError("RAES pack content identity could not be verified") from exc
            if not digest_matches:
                raise CMSError("RAES pack content digest no longer matches registration")
            _launch_pack(
                request_id,
                user,
                pack_root,
                options,
                _runtime_plugin_scope(
                    user,
                    options.workspace_id,
                    source,
                    options.content_authorizer,
                    options.principal_scope,
                    authority_action=options.authority_action,
                    authority_target=options.authority_target,
                ),
                source=source if options.principal_scope is not None else None,
            )
    except RaesPackageError as exc:
        # Staging errors embed server paths and storage provider payloads: log the class only.
        logger.error(
            "create_raes_native_range: object package could not be resolved request_id=%s scenario_id=%s exc_type=%s",
            request_id,
            safe_log_value(source.scenario_id),
            type(exc).__name__,
            exc_info=False,
        )
        raise CMSError("RAES object package could not be resolved") from None


def _object_package_key(package_ref: str) -> str:
    """Join the configured object-package prefix with the row's ``package_ref``."""
    prefix = str(getattr(settings, "RAES_PACKAGE_PREFIX", "") or "").strip().strip("/")
    ref = package_ref.strip().lstrip("/")
    return f"{prefix}/{ref}" if prefix else ref


def _launch_pack(
    request_id: UUID,
    user: User | CredentialContext,
    pack_root: Path,
    options: RaesDispatchOptions,
    plugin_scope: RuntimePluginScope | None,
    *,
    source: RaesPackageSource | None = None,
) -> None:
    """Select the single SDL entry, dispatch through the port, assert acceptance."""
    from cms.raes.dispatch import CmsRaesDispatchPort
    from shared.raes.package_loader import RaesPackageError, launch_raes_package, resolve_pack_scenario_path

    try:
        scenario_path = resolve_pack_scenario_path(pack_root)
    except RaesPackageError as exc:
        # RAES package errors embed server paths: log the class only and keep the text out of the chain.
        logger.error(
            "create_raes_native_range: package scenario could not be resolved request_id=%s exc_type=%s",
            request_id,
            type(exc).__name__,
            exc_info=False,
        )
        raise CMSError("RAES package could not be resolved") from None

    credential = user if isinstance(user, CredentialContext) else None
    scope = options.principal_scope
    port = CmsRaesDispatchPort(
        user_id=user.id if isinstance(user, User) and credential is None else None,
        request_id=str(request_id),
        backend_admission=options.backend_admission,
        pack_root=pack_root,
        workspace_id=options.workspace_id,
        egress_mode=options.egress_mode,
        runtime_plugin_scope=plugin_scope,
        credential=credential,
        scope=scope,
        source=source,
        owner_principal=options.owner_principal,
        authority_action=options.authority_action,
        authority_target=options.authority_target,
    )
    try:
        result = launch_raes_package(
            scenario_path=scenario_path, port=port, artifact_supply_provider=port.artifact_supply
        )
    except RaesPackageError as exc:
        # Launch errors embed scenario, storage provider and server path text: log the class only.
        logger.error(
            "create_raes_native_range: package could not be launched request_id=%s exc_type=%s",
            request_id,
            type(exc).__name__,
            exc_info=False,
        )
        raise CMSError("RAES package could not be launched") from None
    if not result.accepted:
        logger.warning(
            "create_raes_native_range: dispatch not accepted request_id=%s status=%s diagnostic_fingerprints=%s",
            request_id,
            result.status,
            tuple(safe_log_fingerprint(diagnostic) for diagnostic in result.diagnostics),
        )
        raise CMSError("RAES provisioning was not accepted")


def _runtime_plugin_scope(
    user: User | CredentialContext,
    workspace_id: int | None,
    source: RaesPackageSource,
    content_authorizer: User | None,
    principal_scope: ResourceScope | None = None,
    *,
    authority_action: str | None = None,
    authority_target: TargetRef | None = None,
) -> RuntimePluginScope | None:
    """Authorize the workspace and bind the pack's tenant adapter scope."""
    from cms.scenarios.registry import check_scenario_access
    from shared.runtime_plugin_binding import RuntimePluginScope
    from workspaces.services import WorkspaceOperation, authorize_bound_workspace

    if principal_scope is not None:
        from ._scoped_range_launch import revalidate_scoped_launch

        if not isinstance(user, CredentialContext):
            raise CMSError(_RANGE_AUTHORITY_UNAVAILABLE)
        revalidate_scoped_launch(
            user,
            principal_scope,
            source,
            authority_action=authority_action,
            authority_target=authority_target,
        )
        organization_uuid = principal_scope.organization_uuid
        if organization_uuid is None:
            return None
        return RuntimePluginScope(
            organization_uuid=organization_uuid, pack_id=source.scenario_id, pack_digest=source.package_digest
        )
    if not isinstance(user, User):
        raise CMSError(_RANGE_AUTHORITY_UNAVAILABLE)
    authorization = authorize_bound_workspace(user, workspace_id, WorkspaceOperation.LAUNCH_RANGE)
    organization_uuid = authorization.organization_uuid
    if source.organization_uuid is not None and content_authorizer is not None:
        # Re-prove the explicit CTF event owner's tenant-pack access at the
        # dispatch boundary. Keep the range in the participant's personal
        # workspace while binding the adapter in the pack's owning organization.
        check_scenario_access(source.scenario_id, content_authorizer)
        organization_uuid = source.organization_uuid
    elif source.organization_uuid is not None and source.organization_uuid != organization_uuid:
        raise CMSError("The pack is unavailable in this workspace because it belongs to a different organization")
    if organization_uuid is None:
        raise CMSError("The workspace has no organization binding")
    return RuntimePluginScope(
        organization_uuid=organization_uuid,
        pack_id=source.scenario_id,
        pack_digest=source.package_digest,
    )
