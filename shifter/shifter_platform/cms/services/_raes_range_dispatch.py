"""Internal dispatch facade for RAES range launches."""

from __future__ import annotations

from typing import TYPE_CHECKING

from cms.exceptions import CMSError
from cms.services._range_launch_common import LaunchOptions
from shared.credentials import CredentialContext
from shared.identity_scope import PrincipalRef, ResourceScope
from shared.range_instantiation_policy import InstantiationPurpose

if TYPE_CHECKING:
    from datetime import datetime
    from uuid import UUID

    from django.contrib.auth.models import User

    from shared.authorization import TargetRef
    from shared.enums import RangeSource
    from shared.model_access import OwnedReference
    from shared.model_access.reservation import ModelLaunchScope
    from shared.schemas.range import RangeContext


def dispatch_range_launch(
    user: User,
    scenario: str,
    *,
    range_source: RangeSource | None,
    instantiation_purpose: InstantiationPurpose,
    options: LaunchOptions,
) -> RangeContext:
    """Launch with the authority and policy prepared by the caller."""
    from cms.services._raes_range_create import _create_raes_native_range_impl

    return _create_raes_native_range_impl(
        user,
        scenario,
        range_source=range_source,
        instantiation_purpose=instantiation_purpose,
        workspace_uuid=options.workspace_uuid,
        enforced_deadline=options.remote_access_teardown_at,
        model_admission_subject=options.model_admission_subject,
        model_launch_scope=options.model_launch_scope,
        model_sources=options.model_sources,
        content_authorizer=options.content_authorizer,
        ctf_policy_workspace_id=options.ctf_policy_workspace_id,
    )


def create_range_dispatch(  # NOSONAR -- stable cross-service facade retained for existing callers.
    user: User | CredentialContext,  # NOSONAR -- compatibility facade retains its established public signature.
    scenario: str,
    ngfw_enabled: bool = False,
    range_source: RangeSource | None = None,
    remote_access_teardown_at: datetime | None = None,
    workspace_uuid: str | UUID | None = None,
    model_admission_subject: OwnedReference | None = None,
    model_launch_scope: ModelLaunchScope | None = None,
    model_sources: dict | None = None,
    content_authorizer: User | None = None,
    ctf_policy_workspace_id: int | None = None,
    *,
    scope: ResourceScope | None = None,
    owner_principal: PrincipalRef | None = None,
    authority_action: str | None = None,
    authority_target: TargetRef | None = None,
    request_uuid: UUID | None = None,
) -> RangeContext:
    """Launch a registered RAES scenario through the authoritative path.

    ``ngfw_enabled`` remains accepted at the public service seam while callers
    migrate their request shape; RAES packages own topology and authored
    infrastructure intent, so it does not change the plan.

    ``workspace_uuid`` is the optional public workspace selection (ADR-046-R9),
    threaded to whichever create path runs. Server-derived callers (e.g. the CTF
    bridge) omit it, so their ranges bind to the launcher's personal workspace.
    """
    from cms.services._raes_range_create import create_raes_native_range
    from cms.services._scoped_launch_types import NativeEventLaunchOptions

    if isinstance(user, CredentialContext):
        if content_authorizer is not None or ctf_policy_workspace_id is not None:
            raise CMSError("Range launch inputs unavailable")
        return create_raes_native_range(
            user,
            scenario,
            range_source=range_source,
            workspace_uuid=workspace_uuid,
            scope=scope,
            model_sources=model_sources,
            event_options=NativeEventLaunchOptions(
                owner_principal=owner_principal,
                authority_action=authority_action,
                authority_target=authority_target,
                remote_access_teardown_at=remote_access_teardown_at,
                model_admission_subject=model_admission_subject,
                model_launch_scope=model_launch_scope,
                request_uuid=request_uuid,
            ),
        )
    if (
        scope is not None
        or owner_principal is not None
        or authority_action is not None
        or authority_target is not None
        or request_uuid is not None
    ):
        raise CMSError("Range launch scope unavailable")
    return dispatch_range_launch(
        user,
        scenario,
        range_source=range_source,
        instantiation_purpose=InstantiationPurpose.LIVE_FIRE,
        options=LaunchOptions(
            ngfw_enabled=ngfw_enabled,
            remote_access_teardown_at=remote_access_teardown_at,
            workspace_uuid=workspace_uuid,
            model_admission_subject=model_admission_subject,
            model_launch_scope=model_launch_scope,
            model_sources=model_sources,
            content_authorizer=content_authorizer,
            ctf_policy_workspace_id=ctf_policy_workspace_id,
        ),
    )
