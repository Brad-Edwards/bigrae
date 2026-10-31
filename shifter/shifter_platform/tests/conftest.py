"""Pytest configuration and fixtures for Shifter platform tests."""

import logging
import os
import sys
from pathlib import Path

# Add shifter/ to the path for sibling runtime packages.
# Must be done before Django loads settings
SHIFTER_DIR = Path(__file__).resolve().parent.parent.parent
if str(SHIFTER_DIR) not in sys.path:
    sys.path.insert(0, str(SHIFTER_DIR))

# Set testing flag before Django loads settings
os.environ["TESTING"] = "1"
os.environ.setdefault("ENVIRONMENT", "test")
os.environ.setdefault("DJANGO_SECRET_KEY", "shifter-platform-tests-secret-key")

from unittest.mock import MagicMock, Mock  # noqa: E402

import pytest  # noqa: E402
from django.test import Client  # noqa: E402

TESTS_DIR = Path(__file__).parent


@pytest.fixture(autouse=True)
def _reference_drf_force_authenticated_users(monkeypatch):
    """Make DRF's test-only user shortcut carry the same live session proof as production."""
    from django.contrib.auth import BACKEND_SESSION_KEY, HASH_SESSION_KEY, SESSION_KEY
    from django.contrib.auth.models import User
    from rest_framework.test import APIClient

    from config.session_credentials import session_credential
    from management.session_backend import SessionStore

    original = APIClient.force_authenticate

    def force_authenticate(client, user=None, token=None):
        if isinstance(user, User) and token is None:
            session = SessionStore()
            session[SESSION_KEY] = str(user.pk)
            session[BACKEND_SESSION_KEY] = "config.auth.PlatformModelBackend"
            session[HASH_SESSION_KEY] = user.get_session_auth_hash()
            session.save()
            token = session_credential(user, session, provider_check=False)
        return original(client, user=user, token=token)

    monkeypatch.setattr(APIClient, "force_authenticate", force_authenticate)


@pytest.fixture
def personal_token_use_grant(monkeypatch):
    """Add the personal-token grant without replacing the active test policy."""
    from shared.authorization import AuthorizationDecision, DecisionKind, port

    existing_factory = port._provider_factory

    class PersonalTokenGrantProvider:
        def __init__(self, delegate_factory):
            self._delegate_factory = delegate_factory

        def _delegate(self):
            if self._delegate_factory is None:
                raise RuntimeError("No authorization provider is bound")
            return self._delegate_factory()

        def check(self, request):
            if request.action == "installation.use_personal_tokens":
                return AuthorizationDecision(DecisionKind.ALLOWED, "policy_allowed")
            return self._delegate().check(request)

        def __getattr__(self, name):
            return getattr(self._delegate(), name)

    monkeypatch.setattr(
        port,
        "_provider_factory",
        lambda: PersonalTokenGrantProvider(existing_factory),
    )


@pytest.fixture
def personal_vpn_policy_grant(monkeypatch):
    """Grant the exact post-cutover policy decisions used by VPN API fixtures."""
    from types import SimpleNamespace

    from shared.authorization import AuthorizationDecision, DecisionKind, port

    def check(request):
        return AuthorizationDecision(
            DecisionKind.ALLOWED,
            "policy_allowed",
        )

    monkeypatch.setattr(port, "_provider_factory", lambda: SimpleNamespace(check=check))


@pytest.fixture
def allow_all_policy(monkeypatch):
    """Bind a complete allow decision provider for tests below an HTTP boundary."""
    from types import SimpleNamespace

    from shared.authorization import AuthorizationDecision, DecisionKind, port

    def check(_request):
        return AuthorizationDecision(DecisionKind.ALLOWED, "policy_allowed")

    provider = SimpleNamespace(
        store_id="test-store",
        model_id="test-model",
        check=check,
        batch_check=lambda requests: tuple(check(request) for request in requests),
    )
    monkeypatch.setattr(port, "_provider_factory", lambda: provider)
    return provider


@pytest.fixture
def principal_owner_policy(monkeypatch):
    """Allow range reads only to the range's current durable principal owner."""
    from types import SimpleNamespace

    from cms.models import RangeInstance
    from shared.authorization import AuthorizationDecision, DecisionKind, port

    def check(request):
        allowed = (
            request.target.type != "range"
            or RangeInstance.all_objects.filter(
                uuid=request.target.uuid, owner_principal_uuid=request.principal.uuid
            ).exists()
        )
        return AuthorizationDecision(
            DecisionKind.ALLOWED if allowed else DecisionKind.DENIED,
            "policy_allowed" if allowed else "policy_denied",
        )

    provider = SimpleNamespace(
        store_id="test-store",
        model_id="test-model",
        check=check,
        batch_check=lambda requests: tuple(check(request) for request in requests),
    )
    monkeypatch.setattr(port, "_provider_factory", lambda: provider)
    return provider


@pytest.fixture
def live_session_credential(settings):
    """Issue a resolvable non-secret session reference for a test user."""
    from django.contrib.auth import BACKEND_SESSION_KEY, HASH_SESSION_KEY, SESSION_KEY

    from config.session_credentials import session_credential
    from management.session_backend import SessionStore

    settings.SESSION_ENGINE = "management.session_backend"

    def issue(user):
        session = SessionStore()
        session[SESSION_KEY] = str(user.pk)
        session[BACKEND_SESSION_KEY] = "config.auth.PlatformModelBackend"
        session[HASH_SESSION_KEY] = user.get_session_auth_hash()
        session.save()
        return session_credential(user, session, provider_check=False)

    return issue


@pytest.fixture(autouse=True)
def _native_service_credential_audience(settings):
    """Use one explicit synthetic audience for native credential reference tests."""
    if not settings.GCP_SERVICE_TOKEN_AUDIENCE:
        settings.GCP_SERVICE_TOKEN_AUDIENCE = "https://portal.example.test"
    if not settings.AWS_SERVICE_TOKEN_AUDIENCE:
        settings.AWS_SERVICE_TOKEN_AUDIENCE = "https://portal.example.test"


@pytest.fixture(autouse=True)
def _local_backend_for_generic_logins(settings, monkeypatch):
    """Give ``force_login(user)`` without a backend the session a real login makes.

    Provider sessions (Cognito and Identity Platform) are valid only with the
    assurance that a real provider login records. A generic test login must not
    create a provider session that lacks it. Tests that exercise a provider
    session name its backend and record assurance explicitly.

    A temporary CTF participant signs in only through the CTF participant
    backend; the platform backend excludes temporary identities by design.
    """
    from django.test import Client

    from management.services import is_temporary_ctf_account

    backends = list(settings.AUTHENTICATION_BACKENDS)
    local = "config.auth.PlatformModelBackend"
    if local in backends:
        settings.AUTHENTICATION_BACKENDS = [local, *[item for item in backends if item != local]]
    original = Client.force_login

    def force_login(self, user, backend=None):
        # Requests reload the user from the database, so decide from stored state;
        # a test may have changed the profile after it loaded ``user``.
        if backend is None and user.pk is not None:
            stored = type(user)._default_manager.select_related("profile").filter(pk=user.pk).first()
            if stored is not None and is_temporary_ctf_account(stored):
                backend = "config.auth.CTFParticipantBackend"
        return original(self, user, backend)

    monkeypatch.setattr(Client, "force_login", force_login)


@pytest.fixture
def provisioner_workload_config(settings):
    """Install the non-secret native workload identity used by launch tests."""
    settings.CLOUD_PROVIDER = "gcp"
    settings.ENGINE_TASK_CLUSTER = "synthetic-jobs"
    settings.ENGINE_TASK_DEFINITION = "example.test/synthetic-provisioner@sha256:" + "1" * 64
    settings.ENGINE_TASK_SERVICE_ACCOUNT_NAME = "synthetic-provisioner"
    return settings


# -----------------------------------------------------------------------------
# Shared test data for parametrized validation tests
# -----------------------------------------------------------------------------

INVALID_USERS = [
    pytest.param(None, id="none"),
    pytest.param("not-a-user", id="string"),
    pytest.param(Mock(id=None, pk=None), id="unsaved"),
]

INVALID_RANGE_IDS = [
    pytest.param(None, id="none"),
    pytest.param("not-an-id", id="string"),
    pytest.param(-1, id="negative"),
]


# -----------------------------------------------------------------------------
# Shared mock fixtures
# -----------------------------------------------------------------------------


@pytest.fixture
def mock_queryset():
    """Chainable queryset mock for ORM query chains.

    Supports .filter().annotate().order_by().select_related() etc.
    Each method returns the same mock, enabling arbitrary chain lengths.
    """
    qs = MagicMock()
    for method in (
        "filter",
        "exclude",
        "annotate",
        "order_by",
        "select_related",
        "prefetch_related",
        "values_list",
    ):
        getattr(qs, method).return_value = qs
    return qs


@pytest.fixture(autouse=True)
def _reset_secret_cache():
    """Reset the in-process credential cache around every test.

    ``engine.secrets`` keeps a module-global TTL cache of resolved secret values
    (#929). Clearing it before and after each test keeps the global state from
    leaking across tests, so suites that assert on Secrets Manager call counts
    start from a cold cache.
    """
    from engine.secrets import clear_secret_cache

    clear_secret_cache()
    yield
    clear_secret_cache()


@pytest.fixture(autouse=True)
def enable_log_propagation():
    """Enable log propagation for caplog to work with our configured loggers.

    Django settings sets propagate=False on our loggers (engine, cms, ctf,
    config, mission_control) which prevents pytest's caplog from capturing log
    records. This fixture temporarily enables propagation for all tests. The
    names come from the LOGGING config, so a newly configured logger is covered.
    """
    from django.conf import settings

    loggers = [
        name
        for name, config in settings.LOGGING.get("loggers", {}).items()
        if not name.startswith("django") and config.get("propagate") is False
    ]
    original_propagate = {}

    for name in loggers:
        logger = logging.getLogger(name)
        original_propagate[name] = logger.propagate
        logger.propagate = True

    yield

    # Restore original propagation settings
    for name, propagate in original_propagate.items():
        logging.getLogger(name).propagate = propagate


@pytest.fixture
def authenticated_client(db):
    """
    Return a Django test client that bypasses OIDC authentication.

    The OIDC SessionRefresh middleware checks for 'oidc_id_token_expiration'
    in the session. Without it, force_login users get redirected to OIDC.
    This fixture sets up the session properly.
    """
    import time

    from django.contrib.auth import get_user_model

    User = get_user_model()

    def _make_client(user=None, email="test@example.com"):
        if user is None:
            user, _ = User.objects.get_or_create(username=email, defaults={"email": email})

        from shared.principal_port import principal_for_user
        from workspaces.models import Account
        from workspaces.services import create_account

        principal = principal_for_user(user)
        if not Account.objects.filter(kind=Account.Kind.INDIVIDUAL, individual_principal_uuid=principal.uuid).exists():
            create_account(kind=Account.Kind.INDIVIDUAL, name="Individual account", owner=principal)

        client = Client()
        client.force_login(user, backend="config.auth.PlatformModelBackend")

        # Set OIDC session data to prevent SessionRefresh from redirecting
        session = client.session
        session["oidc_id_token_expiration"] = time.time() + 3600  # 1 hour from now
        session.save()

        return client, user

    return _make_client


@pytest.fixture
def shipped_pack_root(tmp_path, settings):
    """Stage the shipped packs at their image destinations and resolve refs there."""
    from tests._environment_packs import stage_shipped_packs

    root = stage_shipped_packs(tmp_path / "app")
    settings.RAES_PACKAGE_ROOT = str(root)
    return root
