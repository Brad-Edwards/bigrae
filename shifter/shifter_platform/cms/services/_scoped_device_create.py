"""CMS device creation resolves private credentials before native admission."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

from django.db import transaction
from django.utils import timezone

from cms.exceptions import CMSError
from cms.models import App, AppType, Credential, Instance, InstanceType, Request
from engine.services import create_ngfw, prepare_ngfw_creation
from shared.credentials import resolve_local_credential
from shared.schemas import RequestSpec
from shared.schemas.app import NGFWAppRef, NGFWOneTimeRegistration
from workspaces.services import resolve_range_scope_binding

from ._content_authority import content_scope, require_content_scope
from ._ngfw_hydration import NGFWRegistration, hydrate_ngfw
from ._scoped_credentials import _authorized_row

if TYPE_CHECKING:
    from shared.credentials import CredentialContext
    from shared.identity_scope import ResourceScope


def _source(
    credential: CredentialContext, scope: ResourceScope | None, identifier: int | None, slug: str
) -> tuple[Credential, dict[str, object]]:
    """Resolve a device input credential within the exact launch scope."""
    if identifier is None:
        raise CMSError("Device source unavailable")
    _, fence = _authorized_row(credential, identifier, "manage_credentials")
    row = Credential.objects.select_related("credential_type").filter(**fence).first()
    if (
        row is None
        or content_scope(row) != scope
        or row.credential_type.slug != slug
        or (row.expires_at is not None and row.expires_at <= timezone.now())
    ):
        raise CMSError("Device source unavailable")
    return row, fence


def create_scoped_device(
    credential: CredentialContext,
    scope: ResourceScope | None,
    name: str,
    profile_id: int,
    method: str,
    scm_id: int | None,
    otp: NGFWOneTimeRegistration | None,
) -> NGFWAppRef:
    """Hydrate native device inputs after admitting customer launch authority."""
    otp_value = otp.value if otp else None
    otp_folder = otp.folder if otp else None

    from management.services import human_principal_user_id

    from ._ngfws import _validate_ngfw_name

    try:
        scope = _device_input_scope(scope, method)
        current = require_content_scope(credential, scope, "manage_devices")
        name = _validate_ngfw_name(name)
        resolved = resolve_range_scope_binding(scope, member=current.principal)
        profile, profile_fence = _source(current, scope, profile_id, "deployment_profile")
        sources = [(profile, profile_fence)]
        scm = None
        if method == "pin":
            scm, scm_fence = _source(current, scope, scm_id, "scm")
            sources.append((scm, scm_fence))
        user_id = human_principal_user_id(current.principal) if current.principal.kind == "human" else None
        request = Request(
            request_id=uuid4(),
            request_type="ngfw",
            user_id=user_id,
            owner_principal_uuid=current.principal.uuid,
            scope_kind=resolved.kind,
            account_id=resolved.account_id,
            organization_id=resolved.organization_id,
            workspace_id=resolved.workspace_id,
        )
        instance = Instance(
            request=request, name=name, instance_type=InstanceType.objects.get(slug="panw-ngfw"), status="provisioning"
        )
        app = App(instance=instance, name=name, app_type=AppType.objects.get(slug="panw-ngfw"), status="provisioning")
        hydrated = hydrate_ngfw(
            instance,
            app,
            request,
            NGFWRegistration(profile, "pin" if method == "pin" else "otp", scm, otp_value, otp_folder),
        )
        spec = RequestSpec(
            request_id=request.request_id,
            user_id=user_id,
            owner_principal_uuid=current.principal.uuid,
            items=[hydrated],
        )
        prepared = prepare_ngfw_creation(spec, current, scope)
        current = require_content_scope(current, scope, "manage_devices")
        with transaction.atomic():
            if (
                resolve_range_scope_binding(scope, member=current.principal, for_update=True) != resolved
                or resolve_local_credential(current) != current
            ):
                raise CMSError("Device authority changed")
            if Request.objects.filter(
                request_type="ngfw",
                owner_principal_uuid=current.principal.uuid,
                scope_kind=resolved.kind,
                account_id=resolved.account_id,
                organization_id=resolved.organization_id,
                workspace_id=resolved.workspace_id,
            ).exists():
                raise CMSError("An active device already exists")
            _assert_device_sources(sources)
            request.save()
            instance.save()
            app.save()
            instance.data = hydrated.model_dump(mode="json")
            instance.save(update_fields=["data"])
            create_ngfw(spec, credential=current, resource_scope=scope, prepared_creation=prepared)
        return NGFWAppRef(app_id=app.pk, instance_id=instance.pk, is_deleted=False)
    except Exception:
        raise CMSError("Device creation unavailable") from None


def _device_input_scope(scope: ResourceScope | None, method: str) -> ResourceScope:
    """Require explicit customer custody and a supported device registration method."""
    if scope is None or scope.kind != "account" or method not in {"otp", "pin"}:
        raise CMSError("Device input unavailable")
    return scope


def _assert_device_sources(sources: list[tuple[Credential, dict[str, object]]]) -> None:
    """Fence the already-admitted encrypted source records while native creation is locked."""
    for source, fence in sources:
        latest = Credential.objects.select_for_update().filter(**fence).first()
        if (
            latest is None
            or latest.data != source.data
            or (latest.expires_at is not None and latest.expires_at <= timezone.now())
        ):
            raise CMSError("Device source changed")
