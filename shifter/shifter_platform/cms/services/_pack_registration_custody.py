"""Bind scoped ingestion to recorded content custody before resolving references."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import UUID

from django.conf import settings
from django.db import transaction

from cms.models import RaesPackageSource, TenantPackArchive
from shared.credentials import CredentialContext
from shared.identity_scope import ResourceScope

from ._content_authority import ContentAuthorityError, content_scope, fence_content_scope, require_content_scope

if TYPE_CHECKING:
    from ._content_ingestion import PackRegistrationRequest
    from ._tenant_pack_upload_journal import PackUploadJournal

_UNAVAILABLE = "Pack registration is unavailable"
_ARCHIVE_FIELDS = (
    "pk",
    "organization_uuid",
    "scenario_id",
    "package_ref",
    "bucket",
    "object_key",
    "location_verified",
    "removal_id",
)
_SOURCE_FIELDS = (
    "pk",
    "owner_principal_uuid",
    "scope_kind",
    "account_uuid",
    "organization_uuid",
    "workspace_uuid",
    "scope_generation",
    "scenario_id",
    "source_kind",
    "package_ref",
)


@dataclass(frozen=True, slots=True)
class RegistrationReferenceAdmission:
    """Invocation-local evidence; HTTP callers cannot supply custody or upload leases."""

    installation_credential: CredentialContext | None = None
    archive: tuple[tuple[str, object], ...] = ()
    source: tuple[tuple[str, object], ...] = ()
    upload: PackUploadJournal | None = None


def _snapshot(row, fields):
    return tuple((name, getattr(row, name)) for name in fields)


def _valid_archive(archive: TenantPackArchive, request: PackRegistrationRequest, scope: ResourceScope) -> bool:
    prefix = settings.RAES_PACKAGE_PREFIX.strip().strip("/")
    key = f"{prefix}/{request.package_ref}" if prefix else request.package_ref
    return (
        scope.organization_uuid is not None
        and scope.workspace_uuid is None
        and archive.organization_uuid == scope.organization_uuid
        and archive.scenario_id == request.scenario_id
        and archive.package_ref == request.package_ref
        and archive.location_verified
        and archive.bucket == settings.RAES_PACKAGE_BUCKET
        and bool(archive.bucket)
        and archive.object_key == key
    )


def _check_upload(credential, request, scope, journal):
    from ._tenant_pack_authority import TenantPackAdmission
    from ._tenant_pack_upload_journal import claim_pack_upload

    claim_pack_upload(journal, TenantPackAdmission(credential, scope.organization_uuid, scope), request)


def prepare_registration_reference(
    credential: CredentialContext,
    request: PackRegistrationRequest,
    scope: ResourceScope,
    upload: PackUploadJournal | None = None,
) -> RegistrationReferenceAdmission:
    """Tenant grants admit recorded references; generic references need installation authority."""
    if scope.kind == "installation":
        return RegistrationReferenceAdmission(installation_credential=credential)
    if request.source_kind == "object":
        archive = TenantPackArchive.objects.filter(package_ref=request.package_ref).first()
        if archive is not None and _valid_archive(archive, request, scope):
            if upload is not None and upload.archive.pk == archive.pk:
                with transaction.atomic():
                    _check_upload(credential, request, scope, upload)
                return RegistrationReferenceAdmission(archive=_snapshot(archive, _ARCHIVE_FIELDS), upload=upload)
            if archive.removal_id is None:
                return RegistrationReferenceAdmission(archive=_snapshot(archive, _ARCHIVE_FIELDS))
        # A recorded archive's scope/location/lifecycle cannot be bypassed with
        # a source row pointing at it, including while cleanup owns the bytes.
        if archive is not None:
            operator = require_content_scope(credential, ResourceScope("installation"), "manage_content")
            return RegistrationReferenceAdmission(installation_credential=operator)
    source = RaesPackageSource.objects.filter(
        scenario_id=request.scenario_id, source_kind=request.source_kind, package_ref=request.package_ref
    ).first()
    if source is not None and content_scope(source) == scope:
        return RegistrationReferenceAdmission(source=_snapshot(source, _SOURCE_FIELDS))
    operator = require_content_scope(credential, ResourceScope("installation"), "manage_content")
    return RegistrationReferenceAdmission(installation_credential=operator)


def fence_registration_reference(
    credential: CredentialContext,
    request: PackRegistrationRequest,
    scope: ResourceScope,
    admission: RegistrationReferenceAdmission,
) -> None:
    """Hold exact recorded custody through create, revision and idempotent reuse."""
    fence_content_scope(credential, scope)
    if admission.installation_credential is not None:
        fence_content_scope(admission.installation_credential, ResourceScope("installation"))
        return
    if admission.archive:
        expected = dict(admission.archive)
        archive_pk = expected.get("pk")
        if type(archive_pk) is not int:
            raise ContentAuthorityError(_UNAVAILABLE)
        archive = TenantPackArchive.objects.select_for_update().filter(pk=archive_pk).first()
        if (
            archive is None
            or _snapshot(archive, _ARCHIVE_FIELDS) != admission.archive
            or not _valid_archive(archive, request, scope)
        ):
            raise ContentAuthorityError(_UNAVAILABLE)
        if admission.upload is not None:
            _check_upload(credential, request, scope, admission.upload)
        elif archive.removal_id is not None:
            raise ContentAuthorityError(_UNAVAILABLE)
        return
    expected = dict(admission.source)
    source_pk = expected.get("pk")
    if not isinstance(source_pk, UUID):
        raise ContentAuthorityError(_UNAVAILABLE)
    source = RaesPackageSource.objects.select_for_update().filter(pk=source_pk).first()
    if (
        source is None
        or _snapshot(source, _SOURCE_FIELDS) != admission.source
        or content_scope(source) != scope
        or TenantPackArchive.objects.filter(package_ref=request.package_ref).exists()
    ):
        raise ContentAuthorityError(_UNAVAILABLE)
