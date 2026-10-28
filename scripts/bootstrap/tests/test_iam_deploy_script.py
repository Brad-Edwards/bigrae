"""Exercise IAM script dispatch with synthetic profiles and command shims."""

import os
import subprocess
from pathlib import Path


def test_all_environments_default_to_plan_and_preserve_lockfile(tmp_path):
    root = tmp_path / "repo"
    scripts = root / "scripts"
    scripts.mkdir(parents=True)
    source = Path(__file__).resolve().parents[3] / "scripts/iam-deploy.sh"
    script = scripts / "iam-deploy.sh"
    script.write_text(source.read_text())
    script.chmod(0o700)
    stack = root / "platform/terraform/global/iam"
    stack.mkdir(parents=True)
    lock = stack / ".terraform.lock.hcl"
    lock.write_text("retain-provider-lock\n")
    backend = tmp_path / "backend.s3.tfbackend"
    backend.write_text('bucket = "synthetic-backend"\n')
    shims = tmp_path / "bin"
    shims.mkdir()
    for name, text in {
        "python3": '#!/bin/sh\nprintf "SHIFTER_BACKEND_CONFIG_PATH=%s\\n" "$TEST_BACKEND"\n',
        "terraform": '#!/bin/sh\nprintf "%s %s\\n" "$AWS_PROFILE" "$*" >> "$TEST_LOG"\n',
    }.items():
        path = shims / name
        path.write_text(text)
        path.chmod(0o700)
    log = tmp_path / "terraform.log"
    environment = {
        "PATH": str(shims) + os.pathsep + os.defpath,
        "TEST_BACKEND": str(backend),
        "TEST_LOG": str(log),
        "PANW_SHIFTER_DEV_PROFILE": "synthetic-dev",
        "PANW_SHIFTER_PROD_PROFILE": "synthetic-prod",
        "TF_INFRA_STATE_BUCKET_DEV": "synthetic-dev-state",
        "TF_INFRA_STATE_BUCKET": "synthetic-prod-state",
    }
    result = subprocess.run(["/bin/bash", str(script), "--env", "all"], env=environment, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert lock.read_text() == "retain-provider-lock\n"
    assert [line.split()[:2] for line in log.read_text().splitlines() if line.split()[1] == "plan"] == [
        ["synthetic-dev", "plan"],
        ["synthetic-prod", "plan"],
    ]
