"""The CI wrapper delegates hard-cut ordering and handles private operator files.

The actual maintenance/probe/activation sequence is exercised by the canonical
bootstrap process tests; this wrapper must not contain another deploy pipeline.
"""

import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[4]
WORKFLOW = REPO_ROOT / ".github/workflows/_gcp-dev.yml"


def _rollout_step():
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    return next(
        step
        for job in workflow["jobs"].values()
        for step in job.get("steps", [])
        if step.get("name") == "Run the rollout stage"
    )


def test_ci_has_one_canonical_rollout_and_no_embedded_cluster_mutations():
    script = _rollout_step()["run"]
    assert script.count('uv run --frozen python deploy.py "${args[@]}"') == 1
    assert "gcp-deploy --inputs" in script and "--stage rollout" in script
    assert "kubectl" not in script and "helm " not in script
    assert "--trusted-repository-id" in script and "--trusted-owner-id" in script
    assert "--identity-cutover-facts" in script and "--identity-bootstrap-facts" in script
    assert "--catalog-credential-file" in script


def test_ci_credentials_are_optional_and_never_projected_into_the_input_bundle():
    step = _rollout_step()
    assert step["env"]["CATALOG_CREDENTIAL"] == "${{ secrets.CATALOG_CREDENTIAL }}"
    adapter = (REPO_ROOT / "scripts/gcp/render_ci_deployment_inputs.py").read_text()
    assert "CATALOG_CREDENTIAL" not in adapter
    assert "unset IDENTITY_CUTOVER_FACTS_JSON IDENTITY_BOOTSTRAP_FACTS_JSON CATALOG_CREDENTIAL" in step["run"]


@pytest.mark.parametrize(
    ("cutover", "bootstrap", "credential", "first", "cli_status"),
    [
        (False, False, False, False, 0),
        (True, True, True, False, 0),
        (True, False, True, False, None),
        (False, True, True, False, None),
        (False, False, True, False, 0),
        (True, True, False, False, 0),
        (False, False, False, True, 73),
        (True, True, True, True, 0),
    ],
)
def test_private_files_delegate_and_cleanup_on_success_or_failure(
    tmp_path, cutover, bootstrap, credential, first, cli_status
):
    binary = tmp_path / "bin"
    binary.mkdir()
    recorder = binary / "uv"
    recorder.write_text(
        """#!/usr/bin/env python3
import hashlib, json, os, stat, sys
from pathlib import Path
args = sys.argv[1:]
proofs = {}
for flag in ('--identity-cutover-facts', '--identity-bootstrap-facts', '--catalog-credential-file'):
    if flag in args:
        path = Path(args[args.index(flag) + 1])
        proofs[flag] = {'mode': stat.S_IMODE(path.stat().st_mode),
                        'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
names = ('IDENTITY_CUTOVER_FACTS_JSON', 'IDENTITY_BOOTSTRAP_FACTS_JSON', 'CATALOG_CREDENTIAL')
Path(os.environ['WRAPPER_CALL']).write_text(json.dumps({'args': args, 'proofs': proofs,
    'raw_environment': any(name in os.environ for name in names)}))
sys.exit(int(os.environ['CLI_STATUS']))
""",
        encoding="utf-8",
    )
    recorder.chmod(0o700)
    values = {
        "--identity-cutover-facts": "synthetic-cutover",
        "--identity-bootstrap-facts": "synthetic-bootstrap",
        "--catalog-credential-file": "synthetic-native-bearer",
    }
    call = tmp_path / "call.json"
    result = subprocess.run(  # noqa: S603 — repository-owned wrapper with local command recorder
        ["/bin/bash", "-c", _rollout_step()["run"]],
        capture_output=True,
        text=True,
        check=False,
        env={
            **os.environ,
            "PATH": f"{binary}:{os.environ['PATH']}",
            "RUNNER_TEMP": str(tmp_path),
            "WRAPPER_CALL": str(call),
            "CLI_STATUS": str(cli_status or 0),
            "DEPLOYMENT_INPUTS": "/synthetic/input",
            "GCP_ENVIRONMENT": "synthetic",
            "TRUSTED_REPOSITORY_ID": "42",
            "TRUSTED_OWNER_ID": "7",
            "GITHUB_RUN_ID": "42",
            "GITHUB_RUN_ATTEMPT": "1",
            "REQUIRE_ACTIVE_CERTIFICATE": "false" if first else "true",
            "IDENTITY_CUTOVER_FACTS_JSON": values["--identity-cutover-facts"] if cutover else "",
            "IDENTITY_BOOTSTRAP_FACTS_JSON": values["--identity-bootstrap-facts"] if bootstrap else "",
            "CATALOG_CREDENTIAL": values["--catalog-credential-file"] if credential else "",
        },
        timeout=10,
    )
    if cli_status is None:
        assert result.returncode != 0 and not call.exists()
    else:
        assert result.returncode == cli_status, result.stderr
        recorded = json.loads(call.read_text())
        assert not recorded["raw_environment"]
        assert ("--first-install" in recorded["args"]) == first
        assert set(recorded["proofs"]) == {
            flag for flag, enabled in zip(values, (cutover, bootstrap, credential), strict=True) if enabled
        }
        for flag, proof in recorded["proofs"].items():
            assert proof == {"mode": 0o600, "sha256": hashlib.sha256(values[flag].encode()).hexdigest()}
        assert all(value not in str(recorded["args"]) for value in values.values())
    assert all(
        not (tmp_path / name).exists()
        for name in ("identity-cutover-facts.json", "identity-bootstrap-facts.json", "catalog-credential")
    )
