"""Management-owned operations for the one-time identity hard cut."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from uuid import UUID

from django.contrib.auth import get_user_model
from django.contrib.sessions.models import Session
from django.db import transaction
from django.utils import timezone

from management.models import Principal, PrincipalConflictError, PrincipalSession, ProviderBinding
from shared.api_tokens.models import ApiToken
from shared.identity_scope import PrincipalRef

from .principals import bind_principal_provider_identity, ensure_human_principal


def identity_cutover_principals() -> dict[int, UUID]:
    """Return the retained legacy-user to principal mapping."""
    return dict(Principal.objects.filter(user_id__isnull=False).values_list("user_id", "uuid"))


def identity_cutover_disposition(
    provider_bindings: Mapping[int, tuple[str, str]],
) -> tuple[dict[str, object], list[dict[str, object]]]:
    """Classify management-owned identity state and provider-binding blockers."""
    principal_by_user = identity_cutover_principals()
    blockers: list[dict[str, object]] = []
    active_humans = Principal.objects.filter(
        kind=Principal.Kind.HUMAN,
        is_active=True,
        user__is_active=True,
        user__profile__is_ctf_account=False,
    )
    bound_principals = set(ProviderBinding.objects.values_list("principal_id", flat=True))
    supplied_users = set(provider_bindings)
    missing = active_humans.exclude(pk__in=bound_principals).exclude(user_id__in=supplied_users).count()
    unknown = supplied_users - set(principal_by_user)
    if missing:
        blockers.append({"reason": "provider_binding_missing", "entity_count": missing})
    if unknown:
        blockers.append({"reason": "provider_binding_principal_missing", "entity_count": len(unknown)})
    tuples = list(provider_bindings.values())
    duplicates = len(tuples) - len(set(tuples))
    collisions = 0
    for user_id, (issuer, subject) in provider_bindings.items():
        existing = ProviderBinding.objects.filter(issuer=issuer, subject=subject).first()
        if existing is not None and existing.principal.uuid != principal_by_user.get(user_id):
            collisions += 1
    if duplicates:
        blockers.append({"reason": "provider_binding_duplicate", "entity_count": duplicates})
    if collisions:
        blockers.append({"reason": "provider_binding_collision", "entity_count": collisions})
    digests = sorted(
        hashlib.sha256(f"{user_id}\0{issuer}\0{subject}".encode()).hexdigest()
        for user_id, (issuer, subject) in provider_bindings.items()
    )
    return (
        {
            "identities": {"principals": Principal.objects.count(), "action": "retain"},
            "provider_bindings": {
                "retained": ProviderBinding.objects.count(),
                "create": len(digests),
                "intent_digest": hashlib.sha256("".join(digests).encode()).hexdigest(),
            },
        },
        blockers,
    )


def apply_identity_provider_bindings(provider_bindings: Mapping[int, tuple[str, str]]) -> None:
    """Create only exact fact-bound provider tuples; never infer from email."""
    for user_id, (issuer, subject) in sorted(provider_bindings.items()):
        principal = Principal.objects.filter(user_id=user_id, kind=Principal.Kind.HUMAN).first()
        if principal is None:
            raise PrincipalConflictError("provider binding principal is unavailable")
        existing = ProviderBinding.objects.filter(issuer=issuer, subject=subject).first()
        if existing is not None:
            if existing.principal_id != principal.pk:
                raise PrincipalConflictError("provider binding tuple conflicts")
            continue
        bind_principal_provider_identity(PrincipalRef(principal.uuid, "human"), issuer, subject)


def bootstrap_principal_uuid(issuer: str, subject: str) -> UUID | None:
    """Resolve one active exact provider tuple for the first administrator grant."""
    return (
        ProviderBinding.objects.filter(
            issuer=issuer,
            subject=subject,
            principal__is_active=True,
        )
        .values_list("principal__uuid", flat=True)
        .first()
    )


def installation_user_count() -> int:
    """Count users, or 0 before the first migration creates the user table (#2679)."""
    user_model = get_user_model()
    from django.db import connection

    if user_model._meta.db_table not in connection.introspection.table_names():
        return 0
    return user_model.objects.count()


def prepare_empty_installation_bootstrap(issuer: str, subject: str) -> UUID | None:
    """Create the first administrator principal on an empty installation (#2671).

    A first install has no user, principal, or provider binding, so the
    bootstrap facts are the only source of the first administrator. This runs
    before the cutover plan, so the plan and its digest include the principal.
    It creates nothing when any user or human principal already exists: a
    retained installation binds its users through the cutover facts instead.
    The username derives from the provider tuple, so a concurrent second run
    fails on the unique username and binding constraints.
    """
    existing = bootstrap_principal_uuid(issuer, subject)
    if existing is not None:
        return existing
    user_model = get_user_model()
    with transaction.atomic():
        if user_model.objects.exists() or Principal.objects.filter(kind=Principal.Kind.HUMAN).exists():
            return None
        username = "principal-" + hashlib.sha256(f"{issuer}\0{subject}".encode()).hexdigest()[:24]
        user = user_model.objects.create_user(username=username, email="", is_active=True)
        principal = ensure_human_principal(user)
        bind_principal_provider_identity(principal, issuer, subject)
        return principal.uuid


def invalidate_pre_cutover_admission() -> None:
    """Revoke all bearer proofs issued before activation and remove sessions."""
    now = timezone.now()
    token_ids = tuple(ApiToken.objects.order_by("created_by_id", "pk").values_list("pk", flat=True))
    for token_id in token_ids:
        token = ApiToken.objects.get(pk=token_id)
        token.revoke()
        ApiToken.objects.filter(pk=token_id).update(verifier_hash="", reissue_required_at=now)
    PrincipalSession.objects.all().delete()
    Session.objects.all().delete()


def identity_cutover_token_count() -> int:
    """Count retained API tokens without exposing their metadata."""
    return ApiToken.objects.count()
