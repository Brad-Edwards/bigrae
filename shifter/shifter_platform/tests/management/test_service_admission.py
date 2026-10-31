"""Service binding is exact, active and independent from human attribution."""

from uuid import uuid4

import pytest

from management import services
from tests._credential_helpers import persisted_credential

pytestmark = pytest.mark.django_db


def test_new_human_has_a_stable_principal(django_user_model):
    user = django_user_model.objects.create_user(username="new-human")
    assert services.principal_for_user(user).kind == "human"


def test_service_admission_resolves_exact_binding_and_survives_creator_loss(django_user_model):
    from management.models import ServiceCredentialAdmission

    creator = django_user_model.objects.create_user(username="attribution-only")
    principal = services.create_service_principal("Synthetic agent", created_by=creator)
    services.bind_principal_provider_identity(principal, "https://accounts.google.com", "123456789012345678901")
    from management.models import ProviderBinding

    binding = ProviderBinding.objects.get(principal__uuid=principal.uuid)
    admission = ServiceCredentialAdmission.objects.create(
        binding=binding, audience="https://portal.example.test", scopes=["ctf:event:read"]
    )
    services.mark_user_deleted(creator)
    assert creator.identity_principal.user_id == creator.pk
    resolved = services.resolve_service_credential(
        issuer=binding.issuer, subject=binding.subject, audience=admission.audience
    )
    assert resolved.principal == principal
    assert resolved.credential_uuid == admission.uuid
    assert resolved.kind == "service"
    for overrides in ({"subject": str(uuid4())}, {"audience": "https://other.example.test"}):
        inputs = {"issuer": binding.issuer, "subject": binding.subject, "audience": admission.audience, **overrides}
        with pytest.raises(services.PrincipalConflictError):
            services.resolve_service_credential(**inputs)
    admission.is_active = False
    admission.save(update_fields=["is_active"])
    with pytest.raises(services.PrincipalConflictError):
        services.resolve_service_credential(issuer=binding.issuer, subject=binding.subject, audience=admission.audience)


@pytest.mark.parametrize("actor_kind", ["session", "service"])
def test_service_administration_audits_the_authorizing_principal(actor_kind, django_user_model, settings, monkeypatch):
    from types import SimpleNamespace

    from management import service_credentials
    from shared.audit.integrity import verify_audit_chain
    from shared.authorization import AuthorizationDecision, CredentialCeiling, DecisionKind, port
    from shared.credentials import CredentialContext
    from shared.models import AuditLog

    user = django_user_model.objects.create_user(username="audit-operator")
    principal = (
        services.principal_for_user(user) if actor_kind == "session" else services.create_service_principal("Operator")
    )
    actor = persisted_credential(
        CredentialContext(
            principal,
            actor_kind,
            uuid4(),
            CredentialCeiling(frozenset({"installation.manage_service_credentials"})),
            frozenset(),
        )
    )
    settings.CLOUD_PROVIDER = "gcp"
    settings.GCP_SERVICE_TOKEN_AUDIENCE = "https://portal.example.test"
    monkeypatch.setattr(
        port,
        "_provider_factory",
        lambda: SimpleNamespace(check=lambda _request: AuthorizationDecision(DecisionKind.ALLOWED, "policy_allowed")),
    )
    before = AuditLog.objects.latest("sequence").sequence
    admitted = service_credentials.create_service_credential(
        actor, name="Agent", subject="123456789012345678901", scopes=["authorization:workspace.read"]
    )
    service_credentials.update_service_principal(actor, admitted["principal_uuid"], is_active=False)
    service_credentials.disable_service_credential(actor, admitted["credential_uuid"])
    rows = list(AuditLog.objects.filter(sequence__gt=before))
    assert len(rows) == 5  # principal, binding, admission, lifecycle, disable
    assert all(row.actor_type == "principal" and row.actor_id is None for row in rows)
    assert all(row.actor_principal_uuid == principal.uuid for row in rows)
    assert all("actor_principal" not in (row.new_state or {}) for row in rows)
    verify_audit_chain()


def test_aws_admission_binds_account_issuer_and_exact_role(django_user_model, settings, monkeypatch):
    from types import SimpleNamespace

    from management import service_credentials
    from management.models import ProviderBinding
    from shared.authorization import AuthorizationDecision, CredentialCeiling, DecisionKind, port
    from shared.credentials import CredentialContext

    issuer = "https://11111111-2222-3333-4444-555555555555.tokens.sts.global.api.aws"
    settings.CLOUD_PROVIDER = "aws"
    settings.AWS_SERVICE_TOKEN_ISSUER = issuer
    settings.AWS_SERVICE_TOKEN_AUDIENCE = "https://portal.example.test"
    settings.AWS_ACCOUNT_ID = "123456789012"
    user = django_user_model.objects.create_user(username="aws-operator")
    actor = persisted_credential(
        CredentialContext(
            services.principal_for_user(user),
            "session",
            uuid4(),
            CredentialCeiling(frozenset({"installation.manage_service_credentials"})),
            frozenset(),
        )
    )
    monkeypatch.setattr(
        port,
        "_provider_factory",
        lambda: SimpleNamespace(check=lambda _request: AuthorizationDecision(DecisionKind.ALLOWED, "policy_allowed")),
    )
    role = "arn:aws:iam::123456789012:role/automation/synthetic-agent"
    admitted = service_credentials.create_service_credential(
        actor, name="AWS agent", subject=role, scopes=["authorization:workspace.read"]
    )
    binding = ProviderBinding.objects.get(principal__uuid=admitted["principal_uuid"])
    assert (binding.issuer, binding.subject) == (issuer, role)
    assert admitted["audience"] == "https://portal.example.test"
    # IAM role paths may hold up to 512 characters; such a role is admitted and persisted.
    deep_role = "arn:aws:iam::123456789012:role/" + "platform/" * 50 + "synthetic-agent"
    assert len(deep_role) > 255
    deep = service_credentials.create_service_credential(
        actor, name="Deep AWS agent", subject=deep_role, scopes=["authorization:workspace.read"]
    )
    assert ProviderBinding.objects.get(principal__uuid=deep["principal_uuid"]).subject == deep_role
    for subject in (
        "123456789012345678901",
        "arn:aws:iam::123456789012:user/synthetic-person",
        "arn:aws:iam::111122223333:role/automation/synthetic-agent",
        "arn:aws:sts::123456789012:assumed-role/synthetic-agent/session",
    ):
        with pytest.raises(ValueError, match="Invalid service admission"):
            service_credentials.create_service_credential(
                actor, name="Refused", subject=subject, scopes=["authorization:workspace.read"]
            )
    settings.AWS_SERVICE_TOKEN_ISSUER = "https://attacker.example.test"
    with pytest.raises(ValueError, match="Invalid service admission"):
        service_credentials.create_service_credential(
            actor, name="Refused", subject=role, scopes=["authorization:workspace.read"]
        )
