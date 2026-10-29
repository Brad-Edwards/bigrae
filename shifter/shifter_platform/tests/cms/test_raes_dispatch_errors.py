"""Package-resolution failures expose authored errors without private diagnostics."""

from types import SimpleNamespace
from uuid import uuid4

import pytest
from django.contrib.auth.models import User

from cms.exceptions import CMSError
from cms.models import RaesPackageSource
from cms.services._raes_dispatch import (
    RaesDispatchOptions,
    _launch_pack,
    dispatch_object_raes_package,
    dispatch_repo_raes_package,
)
from shared.cloud.exceptions import CloudStorageError

_PRIVATE = "SYNTHETIC_PRIVATE_PROVIDER_DETAIL"
_OPTIONS = RaesDispatchOptions(backend_admission=None, workspace_id=None, egress_mode="internet")


@pytest.mark.parametrize("failure", ["repo-root", "object-storage", "scenario-entry", "scenario-parse"])
def test_package_failures_keep_private_material_out_of_errors_and_logs(
    failure, tmp_path, settings, monkeypatch, caplog
):
    settings.RAES_PACKAGE_ROOT = str(tmp_path)
    settings.RAES_PACKAGE_BUCKET = "synthetic-pack-bucket"
    source = RaesPackageSource(scenario_id="synthetic-pack", package_ref=f"../{_PRIVATE}")
    request_id = uuid4()
    user = User(pk=1, username="synthetic-launcher")

    def unavailable(bucket, key):
        raise CloudStorageError(_PRIVATE)

    storage = SimpleNamespace(head_object=unavailable)
    monkeypatch.setattr("shared.cloud.get_object_storage", lambda: storage)
    if failure == "scenario-parse":
        (tmp_path / "sdl").mkdir()
        (tmp_path / "sdl" / "scenario.sdl.yaml").write_text(f"invalid: [{_PRIVATE}\n")
    with pytest.raises(CMSError) as raised:
        if failure == "repo-root":
            dispatch_repo_raes_package(request_id, user, source, _OPTIONS)
        elif failure == "object-storage":
            source.package_ref = "synthetic-pack.tar.gz"
            dispatch_object_raes_package(request_id, user, source, _OPTIONS)
        else:
            _launch_pack(request_id, user, tmp_path, _OPTIONS, None)

    assert _PRIVATE not in str(raised.value)
    assert _PRIVATE not in caplog.text
    assert str(tmp_path) not in str(raised.value)
    assert str(tmp_path) not in caplog.text
    assert raised.value.__cause__ is None
    assert raised.value.__suppress_context__
    [record] = [record for record in caplog.records if record.name == "cms.services._raes_dispatch"]
    assert "exc_type=RaesPackageError" in record.getMessage()
    assert not record.exc_info
