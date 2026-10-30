"""Tests for the standalone OpenFGA model publisher that the deploy Job runs (#2679)."""

import importlib.util
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "openfga_publish", Path(__file__).resolve().parents[1] / "openfga_publish.py"
)
openfga_publish = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(openfga_publish)
MODEL = {"schema_version": "1.1", "type_definitions": [{"type": "principal"}]}


class FakeAdmin:
    """An OpenFGA admin API double for the standalone publisher script."""

    def __init__(self, stores=(), models=()):
        self.stores = list(stores)
        self.models = list(models)
        self.writes: list[tuple[str, str]] = []

    def __call__(self, method, path, body):
        if method == "GET" and path.startswith("/stores?"):
            return {"stores": self.stores, "continuation_token": ""}
        if method == "POST" and path == "/stores":
            self.writes.append(("store", body["name"]))
            return {"id": "NEW-STORE"}
        if method == "GET" and "/authorization-models" in path:
            return {"authorization_models": self.models}
        if method == "POST" and path.endswith("/authorization-models"):
            self.writes.append(("model", path))
            return {"authorization_model_id": "NEW-MODEL"}
        raise AssertionError(f"unexpected {method} {path}")


class TestPublisherScript:
    def test_creates_the_store_and_model_on_first_publication(self):
        admin = FakeAdmin()
        assert openfga_publish.publish(admin, store_name="bigrae-installation", model=MODEL) == {
            "store_id": "NEW-STORE",
            "model_id": "NEW-MODEL",
        }
        assert admin.writes == [("store", "bigrae-installation"), ("model", "/stores/NEW-STORE/authorization-models")]

    def test_reuses_the_store_and_an_equal_model(self):
        published = {
            **MODEL,
            "id": "OLD-MODEL",
            "conditions": {},
            "type_definitions": [{"type": "principal", "metadata": None}],
        }
        admin = FakeAdmin(stores=[{"id": "S", "name": "bigrae-installation"}], models=[published])
        assert openfga_publish.publish(admin, store_name="bigrae-installation", model=MODEL) == {
            "store_id": "S",
            "model_id": "OLD-MODEL",
        }
        assert admin.writes == []

    def test_writes_a_changed_model_and_rejects_duplicate_stores(self):
        changed = {"id": "OLD", "schema_version": "1.1", "type_definitions": [{"type": "group"}]}
        admin = FakeAdmin(stores=[{"id": "S", "name": "bigrae-installation"}], models=[changed])
        assert openfga_publish.publish(admin, store_name="bigrae-installation", model=MODEL)["model_id"] == "NEW-MODEL"
        twins = [{"id": "A", "name": "bigrae-installation"}, {"id": "B", "name": "bigrae-installation"}]
        with pytest.raises(openfga_publish.PublishError, match="more than one"):
            openfga_publish.select_store(twins, "bigrae-installation")
        deleted = [{"id": "A", "name": "bigrae-installation", "deleted_at": "2026-01-01T00:00:00Z"}]
        assert openfga_publish.select_store(deleted, "bigrae-installation") is None
        with pytest.raises(openfga_publish.PublishError, match="https"):
            openfga_publish.https_call("http://openfga-admin", "t", __file__)

    def test_pages_through_stores_and_prints_only_the_binding(self, monkeypatch, tmp_path, capsys):
        pages = iter(
            [
                {"stores": [{"id": "X", "name": "other"}], "continuation_token": "next"},
                {"stores": [{"id": "S", "name": "bigrae-installation"}], "continuation_token": ""},
            ]
        )
        calls = []

        def call(method, path, body):
            calls.append((method, path))
            if path.startswith("/stores?"):
                return next(pages)
            if method == "GET":
                return {"authorization_models": []}
            return {"authorization_model_id": "M"}

        token, model = tmp_path / "token", tmp_path / "model.json"
        token.write_text("secret-admin-token\n")
        model.write_text('{"schema_version": "1.1", "type_definitions": []}')
        monkeypatch.setenv("OPENFGA_TOKEN_FILE", str(token))
        monkeypatch.setenv("OPENFGA_MODEL_FILE", str(model))
        monkeypatch.setenv("OPENFGA_API_URL", "https://openfga-admin.shifter-platform.svc.cluster.local")
        monkeypatch.setenv("OPENFGA_CA_FILE", str(tmp_path / "ca.crt"))
        monkeypatch.setenv("OPENFGA_STORE_NAME", "bigrae-installation")
        monkeypatch.setattr(openfga_publish, "https_call", lambda url, secret, ca: call)
        assert openfga_publish.main() == 0
        assert "continuation_token=next" in calls[1][1]
        output = capsys.readouterr().out
        assert output == 'BIGRAE_OPENFGA_BINDING={"model_id": "M", "store_id": "S"}\n'
        assert "secret-admin-token" not in output
