"""Current range access and independent native-session cleanup on existing work."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING
from uuid import UUID

from django.utils import timezone

from cms.services import admit_device_remote_authority, admit_range_remote_authority
from shared.credentials import CredentialContext
from shared.exceptions import CMSError
from shared.operation_authority import credential_reference_from_record

if TYPE_CHECKING:
    from .guacamole import GuacamoleClient
    from .models import GuacamoleBootstrapRequest

logger = logging.getLogger(__name__)


def renew_gateway_authority(bootstrap_id: object) -> bool:
    """Resolve an opaque gateway binding against its original recorded proof."""
    from .models import GuacamoleBootstrapRequest

    identifier = _bootstrap_reference(bootstrap_id)
    if identifier is None:
        return False
    row = GuacamoleBootstrapRequest.objects.filter(pk=identifier).first()
    if (
        row is None
        or row.termination_pending
        or row.native_terminated_at is not None
        or row.status not in {"running", "succeeded"}
        or (row.is_expired and (row.status == "running" or row.delivered_at is None))
    ):
        return False
    return authorize_bootstrap(row)


def authorize_bootstrap(row: GuacamoleBootstrapRequest, credential: CredentialContext | None = None) -> bool:
    """Reauthorize the original exact proof, with no creator impersonation."""
    if row.authority_kind != "principal" or row.protocol not in {"range_ssh", "rdp", "ngfw_ssh"}:
        return False
    try:
        original = credential_reference_from_record(row)
        if credential is not None and (credential.principal, credential.kind, credential.credential_uuid) != (
            original.principal,
            original.kind,
            original.credential_uuid,
        ):
            raise ValueError("Bootstrap credential changed")
        admit = admit_device_remote_authority if row.protocol == "ngfw_ssh" else admit_range_remote_authority
        context = {"range_uuid": row.authority_target_uuid} if row.protocol != "ngfw_ssh" else {}
        fields = admit(original, row.target_id, **context)
        if credential is not None:
            # Handoff also respects the current HTTP admission's exact ceiling.
            admit(credential, row.target_id, **context)
        return _authority_fields_match(row, fields)
    except (ValueError, CMSError, PermissionError) as exc:
        # Expected denial: a revoked credential, removed authority, or changed target.
        logger.warning(
            "Guacamole bootstrap reauthorization denied: bootstrap_id=%s exc_type=%s",
            row.pk,
            type(exc).__name__,
        )
        return False
    except Exception:
        logger.exception("Guacamole bootstrap reauthorization failed closed: bootstrap_id=%s", row.pk)
        return False


def terminate_bootstrap(
    row: GuacamoleBootstrapRequest, client: GuacamoleClient, *, only_undelivered: bool = False
) -> bool:
    """Cleanup is restricted to the native session recorded on this operation."""
    from .models import GuacamoleBootstrapRequest

    if row.native_terminated_at is not None or not row.native_session_token:
        return False
    candidates = GuacamoleBootstrapRequest.objects.filter(pk=row.pk, native_terminated_at__isnull=True)
    if only_undelivered:
        candidates = candidates.filter(delivered_at__isnull=True, expires_at__lte=timezone.now())
    claimed = candidates.update(termination_pending=True, result_url="", updated_at=timezone.now())
    if not claimed or not client.terminate_session(row.native_session_token):
        return False
    return bool(
        GuacamoleBootstrapRequest.objects.filter(pk=row.pk, native_terminated_at__isnull=True).update(
            native_terminated_at=timezone.now(),
            native_session_token="",
            termination_pending=False,
            # nosec B106
            result_url="",
        )
    )


def revalidate_guacamole_sessions(*, client: GuacamoleClient | None = None, limit: int = 100) -> int:
    """A bounded native controller cycle, including idle and output-only access."""
    from .guacamole_session import _bind_guacamole_client
    from .models import GuacamoleBootstrapRequest

    rows = (
        GuacamoleBootstrapRequest.objects.filter(authority_kind="principal", native_terminated_at__isnull=True)
        # nosec B106
        .exclude(native_session_token="")
        .order_by("updated_at")[: min(max(limit, 1), 500)]
    )
    terminated = 0
    for row in rows:
        expired_handoff = row.delivered_at is None and row.is_expired
        if not expired_handoff and not row.termination_pending and authorize_bootstrap(row):
            GuacamoleBootstrapRequest.objects.filter(pk=row.pk).update(updated_at=timezone.now())
            continue
        gateway = client or _bind_guacamole_client("Remote access", require_signing_key=False)
        terminated += terminate_bootstrap(
            row, gateway, only_undelivered=expired_handoff and not row.termination_pending
        )
    return terminated


def _bootstrap_reference(bootstrap_id: object) -> UUID | None:
    """Require the canonical public spelling of one opaque bootstrap identity."""
    if not isinstance(bootstrap_id, str):
        return None
    try:
        identifier = UUID(bootstrap_id)
    except ValueError:
        return None
    return identifier if str(identifier) == bootstrap_id else None


def _authority_fields_match(row: GuacamoleBootstrapRequest, fields: dict[str, object]) -> bool:
    """Compare only the admitted native authority facts, retaining the original proof separately."""
    return all(getattr(row, name) == value for name, value in fields.items() if name.startswith("authority_"))
