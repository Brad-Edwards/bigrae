"""AWS bootstrap trust follows the repository's selected GitHub subject format."""

import json
import os

import pytest

import deploy
from aws_bootstrap import _github_oidc_trust_policy
from bootstrap_core import BootstrapConfig


def _subject(config):
    policy = _github_oidc_trust_policy(config, "123456789012")
    condition = policy["Statement"][0]["Condition"]
    assert condition["StringEquals"]["token.actions.githubusercontent.com:aud"] == "sts.amazonaws.com"
    return condition["StringLike"]["token.actions.githubusercontent.com:sub"]


def test_immutable_subject_binds_the_resolved_identity(github_identity):
    assert (
        _subject(BootstrapConfig(env="dev", **github_identity)) == "repo:example-org@12345678/example-repo@123456789:*"
    )


@pytest.mark.parametrize(
    ("subject_format", "expected"),
    [
        ("immutable", "repo:example@456/product@123:*"),
        ("default", "repo:example/product:*"),
    ],
)
def test_explicit_format_selects_one_subject(subject_format, expected):
    config = BootstrapConfig(
        env="dev",
        github_org="example",
        github_repo="product",
        github_repository_id="123",
        github_owner_id="456",
        github_subject_format=subject_format,
    )
    assert _subject(config) == expected


@pytest.mark.parametrize(
    "overrides",
    [
        {"github_repository_id": ""},
        {"github_owner_id": "0"},
        {"github_repository_id": "123:*"},
        {"github_repository_id": "1\u0662\u0663"},
        {"github_owner_id": "\u0664\u0665\u0666"},
        {"github_subject_format": "both"},
    ],
)
def test_invalid_identity_fails_before_policy_construction(overrides, github_identity):
    with pytest.raises(ValueError, match="github_"):
        _subject(BootstrapConfig(env="dev", **{**github_identity, **overrides}))


def test_terraform_role_uses_the_bootstrap_identity(tmp_path, monkeypatch, capfd):
    binary = tmp_path / "terraform"
    binary.write_text("#!/usr/bin/env python3\nimport json, sys\nprint(json.dumps(sys.argv[1:]))\n")
    binary.chmod(0o700)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    config = BootstrapConfig(
        env="dev",
        github_org="example",
        github_repo="product",
        github_owner_id="456",
        github_repository_id="123",
    )
    deploy._terraform_apply_iam(config, dry_run=False)
    output = capfd.readouterr().out.splitlines()
    args = json.loads(next(line for line in output if line.startswith('["apply"')))
    identity = dict(arg.removeprefix("-var=").split("=", 1) for arg in args if arg.startswith("-var="))
    assert identity == {
        "github_org": "example",
        "github_repo": "product",
        "github_owner_id": "456",
        "github_repository_id": "123",
        "github_subject_format": "immutable",
    }
