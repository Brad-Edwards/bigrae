"""Native bearer substitution and exact-binding rejection at HTTP admission."""

from uuid import uuid4

import pytest
from rest_framework.exceptions import AuthenticationFailed
from rest_framework.test import APIRequestFactory

from shared.api_tokens.authentication import ApiTokenAuthentication


@pytest.mark.django_db
def test_native_service_bearer_resolves_service_without_a_human(monkeypatch, settings):
    from config import service_identity
    from management import services
    from management.models import ProviderBinding, ServiceCredentialAdmission

    settings.CLOUD_PROVIDER = "gcp"

    settings.GCP_SERVICE_TOKEN_AUDIENCE = "https://portal.example.test"
    principal = services.create_service_principal("Agent")
    services.bind_principal_provider_identity(principal, "https://accounts.google.com", "123456789012345678901")
    ServiceCredentialAdmission.objects.create(
        binding=ProviderBinding.objects.get(principal__uuid=principal.uuid),
        audience=settings.GCP_SERVICE_TOKEN_AUDIENCE,
        scopes=["ctf:event:read"],
    )
    claims = {
        "iss": "https://accounts.google.com",
        "sub": "123456789012345678901",
        "aud": settings.GCP_SERVICE_TOKEN_AUDIENCE,
        "email": "agent@synthetic-project.iam.gserviceaccount.com",
        "email_verified": True,
    }
    monkeypatch.setattr(service_identity.id_token, "verify_oauth2_token", lambda *args, **kwargs: claims)
    request = APIRequestFactory().get("/api/v1/bootstrap/", HTTP_AUTHORIZATION="Bearer header.payload.signature")
    user, credential = ApiTokenAuthentication().authenticate(request)
    assert user is None
    assert credential.principal == principal
    assert request.credential_context == credential
    for substitution in (
        {"aud": "https://other.example.test"},
        {"iss": "https://securetoken.google.com/synthetic-project"},
        {"email_verified": "true"},
        {"sub": str(uuid4())},
    ):
        changed = {**claims, **substitution}
        monkeypatch.setattr(
            service_identity.id_token, "verify_oauth2_token", lambda *args, result=changed, **kwargs: result
        )
        with pytest.raises(AuthenticationFailed):
            ApiTokenAuthentication().authenticate(request)


@pytest.mark.django_db
def test_aws_web_identity_bearer_resolves_admitted_role(monkeypatch, settings):
    from config import service_identity
    from management import services
    from management.models import ProviderBinding, ServiceCredentialAdmission

    issuer = "https://11111111-2222-3333-4444-555555555555.tokens.sts.global.api.aws"
    role = "arn:aws:iam::123456789012:role/automation/synthetic-agent"
    settings.CLOUD_PROVIDER = "aws"
    settings.AWS_SERVICE_TOKEN_ISSUER = issuer
    settings.AWS_SERVICE_TOKEN_AUDIENCE = "https://portal.example.test"
    settings.AWS_ACCOUNT_ID = "123456789012"
    principal = services.create_service_principal("AWS agent")
    services.bind_principal_provider_identity(principal, issuer, role)
    ServiceCredentialAdmission.objects.create(
        binding=ProviderBinding.objects.get(principal__uuid=principal.uuid),
        audience="https://portal.example.test",
        scopes=["ctf:event:read"],
    )
    seen = {}

    def verified(token, **kwargs):
        seen.update(kwargs)
        return role

    monkeypatch.setattr(service_identity, "verify_aws_web_identity_token", verified)
    request = APIRequestFactory().get("/api/v1/bootstrap/", HTTP_AUTHORIZATION="Bearer header.payload.signature")
    user, credential = ApiTokenAuthentication().authenticate(request)
    assert user is None
    assert credential.principal == principal
    assert seen == {"issuer": issuer, "audience": "https://portal.example.test", "account_id": "123456789012"}

    def denied(token, **kwargs):
        raise ValueError("AWS web identity denied")

    monkeypatch.setattr(service_identity, "verify_aws_web_identity_token", denied)
    with pytest.raises(AuthenticationFailed):
        ApiTokenAuthentication().authenticate(request)
    monkeypatch.setattr(service_identity, "verify_aws_web_identity_token", verified)
    settings.AWS_SERVICE_TOKEN_ISSUER = ""
    with pytest.raises(AuthenticationFailed):
        ApiTokenAuthentication().authenticate(request)
