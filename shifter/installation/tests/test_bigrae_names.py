"""The product rename keeps one validated installation contract."""

import pytest

from installation.cli import main
from installation.cli_parser import build_parser
from installation.scaffold import ScaffoldError, scaffold_config


def test_bigrae_config_is_the_default(tmp_path, monkeypatch, write_config, aws_config, capsys):
    source = write_config(aws_config, name="bigrae.yaml")
    monkeypatch.chdir(tmp_path)
    assert main(["validate"]) == 0
    assert source.name in capsys.readouterr().out
    assert build_parser().prog == "bigrae-config"


def test_legacy_config_still_validates(tmp_path, monkeypatch, write_config, aws_config):
    write_config(aws_config)
    monkeypatch.chdir(tmp_path)
    assert main(["validate"]) == 0


def test_conflicting_default_configs_fail_without_values(tmp_path, monkeypatch, write_config, aws_config, capsys):
    write_config(aws_config)
    aws_config["deployment"]["name"] = "private-deployment"
    write_config(aws_config, name="bigrae.yaml")
    monkeypatch.chdir(tmp_path)
    assert main(["validate"]) == 1
    error = capsys.readouterr().err
    assert "conflicting" in error
    assert "private-deployment" not in error


def test_explicit_path_does_not_consult_other_defaults(tmp_path, monkeypatch, write_config, aws_config):
    write_config(aws_config, name="bigrae.yaml")
    write_config(raw="invalid: [", name="shifter.yaml")
    monkeypatch.chdir(tmp_path)
    assert main(["validate", "bigrae.yaml"]) == 0


def test_scaffold_does_not_create_a_second_default_beside_existing_legacy_config(
    tmp_path, monkeypatch, write_config, aws_config
):
    write_config(aws_config)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ScaffoldError, match="legacy"):
        scaffold_config("aws")
    assert not (tmp_path / "bigrae.yaml").exists()
