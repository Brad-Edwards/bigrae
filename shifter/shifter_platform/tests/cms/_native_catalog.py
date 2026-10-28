"""Actual issued native proof for catalog behavior tests."""

from types import SimpleNamespace

import pytest

from shared.api_tokens.models import ApiToken
from shared.api_tokens.policy import personal_credential_for_token
from shared.identity_scope import ResourceScope

CATALOG_SCOPES = [
    "authorization:installation.read_content",
    "authorization:installation.manage_content",
    "authorization:installation.publish_content",
]


@pytest.fixture
def native_catalog(django_user_model, tmp_path, allow_all_policy):
    user = django_user_model.objects.create_user(username="native-catalog-operator")
    token, raw = ApiToken.create_token(name="Synthetic catalog operator", created_by=user, scopes=CATALOG_SCOPES)
    path = tmp_path / "catalog-credential"
    path.write_text(raw, encoding="ascii")
    path.chmod(0o600)
    return SimpleNamespace(
        user=user,
        token=token,
        credential=personal_credential_for_token(token),
        scope=ResourceScope("installation"),
        path=path,
        provider=allow_all_policy,
    )
