"""Durable credential references retain live lifecycle and original limits."""

from datetime import timedelta
from uuid import uuid4

import pytest
from django.utils import timezone

from shared.authorization import CredentialCeiling, TargetRef
from shared.credentials import CredentialContext

pytestmark = pytest.mark.postgres


@pytest.mark.django_db(transaction=True)
def test_personal_token_commit_fence_checks_local_lifecycle_without_provider_io(django_user_model, monkeypatch):
    from types import SimpleNamespace

    from django.db import transaction

    from shared.api_tokens.models import ApiToken
    from shared.api_tokens.policy import personal_credential_for_token
    from shared.authorization import AuthorizationDecision, DecisionKind, port
    from shared.credentials import resolve_current_credential, resolve_local_credential

    user = django_user_model.objects.create_user(username="personal-commit-fence")
    token, _ = ApiToken.create_token(
        name="Local fence", created_by=user, scopes=["authorization:installation.read_audit"]
    )
    calls = []

    def check(request):
        assert not transaction.get_connection().in_atomic_block
        calls.append(request.action)
        return AuthorizationDecision(DecisionKind.ALLOWED, "policy_allowed")

    monkeypatch.setattr(port, "_provider_factory", lambda: SimpleNamespace(check=check))
    original = personal_credential_for_token(token)
    admitted = resolve_current_credential(original)
    assert calls
    calls.clear()
    with transaction.atomic():
        assert resolve_local_credential(admitted) == admitted
    assert calls == []
    token.revoke()
    with transaction.atomic(), pytest.raises(ValueError, match="Credential unavailable"):
        resolve_local_credential(admitted)


@pytest.mark.django_db(transaction=True)
def test_transaction_credential_fence_checks_local_lifecycle_without_provider_io(
    client, django_user_model, settings, monkeypatch
):
    import time
    from types import SimpleNamespace

    from django.db import transaction

    from config.session_credentials import establish_identity_session, session_credential
    from management.services import bind_provider_identity
    from shared.credentials import resolve_current_credential, resolve_local_credential

    settings.SESSION_ENGINE = "management.session_backend"
    settings.IDENTITY_PLATFORM_PROJECT_ID = "example"
    settings.IDENTITY_SESSION_RECHECK_SECONDS = 1
    settings.AUTHENTICATION_BACKENDS = ["config.identity_platform.IdentityPlatformBackend"]
    user = django_user_model.objects.create_user(username="transaction-session-fence")
    bind_provider_identity(user, "https://securetoken.google.com/example", "subject")
    client.force_login(user, backend="config.identity_platform.IdentityPlatformBackend")
    session = client.session
    timestamp = time.time() - 10
    establish_identity_session(
        session,
        {"iss": "https://securetoken.google.com/example", "sub": "subject", "auth_time": timestamp},
        now=timestamp,
    )
    session.save()
    calls = []

    def provider_state(subject):
        assert not transaction.get_connection().in_atomic_block
        calls.append(subject)
        return SimpleNamespace(disabled=False, email_verified=True, tokens_valid_after_timestamp=0)

    monkeypatch.setattr("config.identity_platform.provider_user_state", provider_state)
    original = session_credential(user, session)
    admitted = resolve_current_credential(original)
    assert calls
    calls.clear()
    with transaction.atomic():
        assert resolve_local_credential(admitted) == admitted
    assert calls == []
    client.logout()
    with transaction.atomic(), pytest.raises(ValueError, match="Credential unavailable"):
        resolve_local_credential(admitted)


@pytest.mark.django_db
def test_session_reference_is_stable_secret_free_and_dies_on_logout(client, django_user_model, settings):
    from config.session_credentials import session_credential
    from shared.credentials import resolve_current_credential

    settings.SESSION_ENGINE = "management.session_backend"
    user = django_user_model.objects.create_user(username="durable-session")
    client.force_login(user, backend="config.auth.PlatformModelBackend")
    session = client.session
    credential = session_credential(user, session)
    assert credential.credential_uuid != credential.principal.uuid
    assert session.session_key not in repr(credential)
    session["display_preference"] = "compact"
    session.save()
    assert session_credential(user, session).credential_uuid == credential.credential_uuid
    assert resolve_current_credential(credential) == credential
    client.logout()
    with pytest.raises(ValueError, match="Credential unavailable"):
        resolve_current_credential(credential)


@pytest.mark.django_db
def test_session_rotation_expiry_and_principal_disable_deny_old_work(client, django_user_model, settings):
    from config.session_credentials import session_credential
    from management.models import PrincipalSession
    from shared.credentials import resolve_current_credential

    settings.SESSION_ENGINE = "management.session_backend"
    user = django_user_model.objects.create_user(username="rotated-session")
    client.force_login(user, backend="config.auth.PlatformModelBackend")
    session = client.session
    original = session_credential(user, session)
    session.cycle_key()
    rotated = session_credential(user, session)
    assert original.credential_uuid != rotated.credential_uuid
    with pytest.raises(ValueError):
        resolve_current_credential(original)
    PrincipalSession.objects.filter(credential_uuid=rotated.credential_uuid).update(
        expire_date=timezone.now() - timedelta(seconds=1)
    )
    with pytest.raises(ValueError):
        resolve_current_credential(rotated)
    client.force_login(user, backend="config.auth.PlatformModelBackend")
    current = session_credential(user, client.session)
    user.identity_principal.is_active = False
    user.identity_principal.save(update_fields=["is_active"])
    with pytest.raises(ValueError):
        resolve_current_credential(current)


@pytest.mark.django_db
def test_session_reference_cannot_be_rebound_to_another_principal(client, django_user_model, settings):
    from config.session_credentials import session_credential
    from shared.credentials import resolve_current_credential

    settings.SESSION_ENGINE = "management.session_backend"
    user = django_user_model.objects.create_user(username="original-session")
    other = django_user_model.objects.create_user(username="other-session")
    client.force_login(user, backend="config.auth.PlatformModelBackend")
    credential = session_credential(user, client.session)
    from management.services import principal_for_user

    substituted = CredentialContext(
        principal_for_user(other), credential.kind, credential.credential_uuid, credential.ceiling, credential.scopes
    )
    with pytest.raises(ValueError):
        resolve_current_credential(substituted)


@pytest.mark.django_db
def test_service_reference_revalidates_admission_and_cannot_widen_queued_ceiling(settings):
    from management.models import Principal, ProviderBinding, ServiceCredentialAdmission
    from management.services import create_service_principal, resolve_service_credential
    from shared.credentials import resolve_current_credential

    principal = create_service_principal("Independent executor")
    binding = ProviderBinding.objects.create(
        principal=Principal.objects.get(uuid=principal.uuid),
        issuer="https://accounts.google.com",
        subject="synthetic-workload",
    )
    admission = ServiceCredentialAdmission.objects.create(
        binding=binding,
        audience="https://example.test/shifter",
        scopes=["authorization:range.read", "authorization:range.manage"],
    )
    settings.GCP_SERVICE_TOKEN_AUDIENCE = admission.audience
    current = resolve_service_credential(issuer=binding.issuer, subject=binding.subject, audience=admission.audience)
    target = TargetRef("range", uuid4())
    original = CredentialContext(
        current.principal,
        current.kind,
        current.credential_uuid,
        CredentialCeiling(frozenset({"range.read"}), target),
        frozenset({"authorization:range.read"}),
    )
    resolved = resolve_current_credential(original)
    assert resolved.ceiling == original.ceiling
    assert not resolved.ceiling.permits("range.manage", target)
    assert not resolved.ceiling.permits("range.read", TargetRef("range", uuid4()))
    admission.is_active = False
    admission.save(update_fields=["is_active"])
    with pytest.raises(ValueError):
        resolve_current_credential(original)


@pytest.mark.django_db
def test_socket_provider_recheck_does_not_renew_framework_or_identity_idle_expiry(
    client, django_user_model, settings, monkeypatch
):
    import time
    from types import SimpleNamespace

    from config.session_credentials import establish_identity_session, validate_socket_session
    from management.models import PrincipalSession
    from management.services import bind_provider_identity

    settings.SESSION_ENGINE = "management.session_backend"
    settings.IDENTITY_PLATFORM_PROJECT_ID = "example"
    settings.IDENTITY_SESSION_RECHECK_SECONDS = 1
    settings.AUTHENTICATION_BACKENDS = ["config.identity_platform.IdentityPlatformBackend"]
    user = django_user_model.objects.create_user(username="nonrenewing-socket")
    bind_provider_identity(user, "https://securetoken.google.com/example", "subject")
    client.force_login(user, backend="config.identity_platform.IdentityPlatformBackend")
    session = client.session
    timestamp = time.time() - 10
    establish_identity_session(
        session,
        {"iss": "https://securetoken.google.com/example", "sub": "subject", "auth_time": timestamp},
        now=timestamp,
    )
    session.set_expiry(60)
    session.save()
    reference = session.credential_uuid
    previous_expiry = PrincipalSession.objects.get(credential_uuid=reference).expire_date
    monkeypatch.setattr(
        "config.identity_platform.provider_user_state",
        lambda subject: SimpleNamespace(disabled=False, email_verified=True, tokens_valid_after_timestamp=0),
    )
    scope = {"session": session}
    assert validate_socket_session(scope, touch=False)
    row = PrincipalSession.objects.get(credential_uuid=reference)
    assert row.expire_date == previous_expiry
    assert row.get_decoded()["identity_assurance"]["seen"] == timestamp


@pytest.mark.django_db
def test_http_admission_uses_the_live_session_reference_with_source_backend(client, django_user_model, settings):
    from types import SimpleNamespace

    from shared.api.principals import authenticated_credential
    from shared.credentials import resolve_current_credential

    settings.SESSION_ENGINE = "management.session_backend"
    user = django_user_model.objects.create_user(username="http-session-reference")
    client.force_login(user, backend="config.auth.PlatformModelBackend")
    session = client.session
    credential = authenticated_credential(SimpleNamespace(user=user, session=session, auth=None))
    assert credential.credential_uuid == session.credential_uuid
    assert credential.credential_uuid != credential.principal.uuid
    assert resolve_current_credential(credential) == credential
    client.logout()
    with pytest.raises(ValueError):
        resolve_current_credential(credential)


@pytest.mark.django_db
@pytest.mark.parametrize("field", ["credential_uuid", "principal_uuid", "session_key"])
def test_session_reference_bulk_rebinding_is_rejected(client, django_user_model, settings, field):
    from django.db import IntegrityError, transaction

    from management.models import PrincipalSession
    from management.services import principal_for_user

    settings.SESSION_ENGINE = "management.session_backend"
    user = django_user_model.objects.create_user(username="immutable-session-reference")
    other = django_user_model.objects.create_user(username="substitute-session-reference")
    client.force_login(user, backend="config.auth.PlatformModelBackend")
    session = client.session
    values = {
        "credential_uuid": uuid4(),
        "principal_uuid": principal_for_user(other).uuid,
        "session_key": "replacement",
    }
    with pytest.raises(IntegrityError), transaction.atomic():
        PrincipalSession.objects.filter(session_key=session.session_key).update(**{field: values[field]})
    assert PrincipalSession.objects.get(session_key=session.session_key).principal_uuid == principal_for_user(user).uuid


@pytest.mark.django_db
def test_socket_keeps_original_session_and_exact_ceiling(client, django_user_model, settings):
    from config.session_credentials import session_credential, validate_socket_session

    settings.SESSION_ENGINE = "management.session_backend"
    user = django_user_model.objects.create_user(username="socket-pinned-reference")
    client.force_login(user, backend="config.auth.PlatformModelBackend")
    session = client.session
    admitted = session_credential(user, session)
    original = CredentialContext(
        admitted.principal,
        admitted.kind,
        admitted.credential_uuid,
        CredentialCeiling(frozenset({"range.read"}), TargetRef("range", uuid4())),
        admitted.scopes,
    )
    scope = {"session": session, "credential_context": original}
    assert validate_socket_session(scope)
    assert scope["credential_context"] == original
    session.cycle_key()
    scope["session"] = session
    assert not validate_socket_session(scope)


@pytest.mark.django_db
def test_service_socket_checks_its_original_native_admission(settings):
    from config.session_credentials import validate_socket_session
    from management.models import Principal, ProviderBinding, ServiceCredentialAdmission
    from management.services import create_service_principal, resolve_service_credential

    principal = create_service_principal("Synthetic socket service")
    binding = ProviderBinding.objects.create(
        principal=Principal.objects.get(uuid=principal.uuid), issuer="https://accounts.google.com", subject=str(uuid4())
    )
    settings.GCP_SERVICE_TOKEN_AUDIENCE = "https://example.test/shifter"
    admission = ServiceCredentialAdmission.objects.create(
        binding=binding, audience=settings.GCP_SERVICE_TOKEN_AUDIENCE, scopes=["authorization:range.read"]
    )
    credential = resolve_service_credential(
        issuer=binding.issuer, subject=binding.subject, audience=settings.GCP_SERVICE_TOKEN_AUDIENCE
    )
    scope = {"credential_context": credential}
    assert validate_socket_session(scope)
    admission.is_active = False
    admission.save(update_fields=["is_active"])
    assert not validate_socket_session(scope)
