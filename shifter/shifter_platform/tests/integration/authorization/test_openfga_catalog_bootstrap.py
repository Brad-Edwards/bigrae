"""Native catalog CLI against actual proof records, PostgreSQL and released OpenFGA."""

from uuid import uuid4

import pytest
from django.conf import settings
from django.core.management import CommandError, call_command

from cms.models import RaesPackageSource
from shared.api_tokens.models import ApiToken
from shared.authorization import PolicyEffect, PolicyRelationshipChange, RelationshipSubject, TargetRef, port
from shared.models import AuditLog
from tests.cms._native_catalog import CATALOG_SCOPES
from tests.integration.authorization.test_openfga_released_server import provider as provider

pytestmark = [pytest.mark.openfga, pytest.mark.postgres, pytest.mark.django_db(transaction=True)]


@pytest.mark.parametrize("kind", ["personal", "service"])
def test_real_provider_native_catalog_bootstrap(
    provider, django_user_model, live_session_credential, shipped_pack_root, tmp_path, monkeypatch, kind
):
    monkeypatch.setattr(port, "_provider_factory", lambda: provider)
    user = django_user_model.objects.create_user(username=f"catalog-{uuid4()}")
    human = live_session_credential(user)
    provider.write_relationships(
        PolicyRelationshipChange(
            RelationshipSubject("principal", human.principal.uuid),
            "installation.use_personal_tokens",
            TargetRef("installation"),
            PolicyEffect.GRANT,
        )
    )
    if kind == "personal":
        proof_record, proof = ApiToken.create_token(
            name="Native catalog operator", created_by=user, scopes=CATALOG_SCOPES
        )
        principal_uuid = human.principal.uuid
    else:
        from config import service_identity
        from management.models import ServiceCredentialAdmission
        from management.service_credentials import create_service_credential

        # The synthetic subject and proof are Google service-account values.
        monkeypatch.setattr(settings, "CLOUD_PROVIDER", "gcp")
        provider.write_relationships(
            PolicyRelationshipChange(
                RelationshipSubject("principal", human.principal.uuid),
                "installation.manage_service_credentials",
                TargetRef("installation"),
                PolicyEffect.GRANT,
            )
        )
        admission = create_service_credential(
            human, name="Native catalog service", subject="123456789012345678901", scopes=CATALOG_SCOPES
        )
        proof_record = ServiceCredentialAdmission.objects.get(uuid=admission["credential_uuid"])
        principal_uuid = admission["principal_uuid"]
        claims = {
            "iss": proof_record.binding.issuer,
            "sub": proof_record.binding.subject,
            "aud": settings.GCP_SERVICE_TOKEN_AUDIENCE,
            "email": "catalog@synthetic-project.iam.gserviceaccount.com",
            "email_verified": True,
        }
        monkeypatch.setattr(service_identity.id_token, "verify_oauth2_token", lambda *args, **kwargs: claims)
        proof = "header.payload.signature"
    path = tmp_path / "native-credential"
    path.write_text(proof)
    path.chmod(0o600)
    with pytest.raises(CommandError):
        call_command("bootstrap_inbox_catalog", credential_file=str(path))
    assert not RaesPackageSource.objects.exists()
    for action in ("read_content", "manage_content", "publish_content"):
        provider.write_relationships(
            PolicyRelationshipChange(
                RelationshipSubject("principal", principal_uuid),
                f"installation.{action}",
                TargetRef("installation"),
                PolicyEffect.GRANT,
            )
        )
    call_command("bootstrap_inbox_catalog", credential_file=str(path))
    sources = list(RaesPackageSource.objects.all())
    assert len(sources) == 2
    assert all(source.scope_kind == "installation" and source.is_operator_published for source in sources)
    assert all(source.conformance_status == "passed" and source.registered_by_id is None for source in sources)
    assert {source.owner_principal_uuid for source in sources} == {principal_uuid}
    assert AuditLog.objects.filter(actor_principal_uuid=principal_uuid, entity_type="scenario").exists()
    identities = {source.pk for source in sources}
    call_command("bootstrap_inbox_catalog", credential_file=str(path))
    assert set(RaesPackageSource.objects.values_list("pk", flat=True)) == identities
    if kind == "personal":
        proof_record.revoke()
    else:
        ServiceCredentialAdmission.objects.filter(pk=proof_record.pk).update(is_active=False)
    with pytest.raises(CommandError):
        call_command("bootstrap_inbox_catalog", credential_file=str(path))
    assert set(RaesPackageSource.objects.values_list("pk", flat=True)) == identities
