"""Native device entrypoints and safe status projections."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

from cms.exceptions import CMSError
from shared.identity_scope import ResourceScope

from ._content_authority import require_native_content_credential

if TYPE_CHECKING:
    from cms.models import App
    from shared.schemas.app import NGFWAppContext, NGFWAppRef, NGFWOneTimeRegistration


def _app_to_ngfw_context(app: App) -> NGFWAppContext:
    """Convert App model to NGFWAppContext projection.

    Internal helper - do not call from outside cms.services.
    AWS infrastructure details are owned by Engine, not exposed here.

    Args:
        app: App model with ``instance`` and ``instance.request`` loaded.
    """
    from shared.schemas.app import NGFWAppContext

    assert app.instance is not None, "App must have an instance"
    return NGFWAppContext(
        app_id=app.id,
        instance_id=app.instance.id,
        request_id=app.instance.request.request_id,
        name=app.name,
        status=app.status,
        created_at=app.created_at,
        serial_number=app.data.get("serial_number"),
    )


def _validate_ngfw_name(name: str) -> str:
    """Strip and require a non-empty NGFW display name."""
    if not name or not name.strip():
        raise ValueError("name is required")
    return name.strip()


def list_ngfws(user: object, *, scope: ResourceScope | None = None) -> list[NGFWAppContext]:
    from ._scoped_devices import list_scoped_devices

    return list_scoped_devices(require_native_content_credential(user), scope)


def get_ngfw(user: object, app_id: UUID | str) -> NGFWAppContext:
    from ._scoped_devices import get_scoped_device

    return get_scoped_device(require_native_content_credential(user), app_id)


def create_ngfw(
    user: object,
    name: str,
    deployment_profile_id: int,
    registration_method: str,
    scm_credential_id: int | None = None,
    otp: NGFWOneTimeRegistration | None = None,
    *,
    scope: ResourceScope | None = None,
) -> NGFWAppRef:
    from ._scoped_device_create import create_scoped_device

    credential = require_native_content_credential(user)
    if registration_method not in {"otp", "pin"}:
        raise CMSError("Invalid registration method")
    return create_scoped_device(
        credential, scope, name, deployment_profile_id, registration_method, scm_credential_id, otp
    )


def destroy_ngfw(user: object, app_id: UUID | str, confirm_name: str) -> NGFWAppRef:
    from ._scoped_devices import destroy_scoped_device

    return destroy_scoped_device(require_native_content_credential(user), app_id, confirm_name)
