"""Exercise the real entrypoint across its secret-fetch/process boundary."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ENTRYPOINT = Path(__file__).resolve().parents[1] / "entrypoint.sh"


def test_native_entrypoint_hydrates_app_secrets_without_fetching_database_passwords(tmp_path):
    binary = tmp_path / "bin"
    binary.mkdir()
    dispatcher = binary / "python"
    dispatcher.write_text(
        f"#!{sys.executable}\n"
        "import json, os, subprocess, sys\n"
        "if sys.argv[1] == '-':\n"
        "    sys.stdin.read()\n"
        "    if sys.argv[2] != 'projects/synthetic-project/secrets/app': sys.exit(83)\n"
        "    print(json.dumps({'django_secret_key': 'synthetic-app-key', 'field_encryption_key': 'eA=='}))\n"
        "elif sys.argv[1] == '-c' and 'psycopg.connect(' in sys.argv[2]:\n"
        "    assert not os.environ.get('DB_PASSWORD')\n"
        "else:\n"
        f"    sys.exit(subprocess.call([{sys.executable!r}, *sys.argv[1:]]))\n"
    )
    dispatcher.chmod(0o700)
    capture = tmp_path / "capture.py"
    output = tmp_path / "observed.json"
    capture.write_text(
        "import json, os\nfrom pathlib import Path\n"
        f"Path({str(output)!r}).write_text(json.dumps({{k: os.environ.get(k) for k in "
        "['DJANGO_SECRET_KEY', 'DB_AUTH_MODE', 'DB_PASSWORD', 'PROVISIONER_DB_USER', 'PROVISIONER_DB_PASSWORD']}))\n"
    )
    result = subprocess.run(  # noqa: S603 -- fixed repository entrypoint with local synthetic process fixtures
        ["/bin/bash", str(ENTRYPOINT), sys.executable, str(capture)],
        env={
            "PATH": str(binary) + os.pathsep + os.defpath,
            "CLOUD_PROVIDER": "gcp",
            "DB_AUTH_MODE": "cloud-sql-proxy",
            "DB_HOST": "127.0.0.1",
            "DB_PORT": "5432",
            "DB_NAME": "shifter",
            "DB_USER": "synthetic-portal@synthetic-project.iam",
            "APP_SECRET_ID": "projects/synthetic-project/secrets/app",
            "SKIP_MIGRATIONS": "1",
            "SHIFTER_IDENTITY_MAINTENANCE": "true",
            "SHIFTER_PROVISIONER_LAUNCHER": "true",
            "PROVISIONER_DB_HOST": "127.0.0.1",
            "PROVISIONER_DB_PORT": "5432",
            "PROVISIONER_DB_NAME": "shifter",
            "PROVISIONER_DB_USER": "synthetic-provisioner@synthetic-project.iam",
        },
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    observed = json.loads(output.read_text())
    assert observed["DJANGO_SECRET_KEY"] == "synthetic-app-key"
    assert observed["DB_AUTH_MODE"] == "cloud-sql-proxy"
    assert observed["DB_PASSWORD"] is None and observed["PROVISIONER_DB_PASSWORD"] is None
    assert observed["PROVISIONER_DB_USER"] == "synthetic-provisioner@synthetic-project.iam"
    assert "synthetic-app-key" not in result.stdout + result.stderr


def _aws_rds_iam_run(tmp_path, **overrides):
    binary = tmp_path / "bin"
    binary.mkdir(exist_ok=True)
    dispatcher = binary / "python"
    dispatcher.write_text(
        f"#!{sys.executable}\n"
        "import json, os, subprocess, sys\n"
        "if sys.argv[1] == '-':\n"
        "    sys.stdin.read()\n"
        "    print(json.dumps({'django_secret_key': 'synthetic-app-key', 'field_encryption_key': 'eA=='}))\n"
        "elif sys.argv[1] == '-c' and 'psycopg.connect(' in sys.argv[2]:\n"
        "    assert not os.environ.get('DB_PASSWORD')\n"
        "    assert 'generate_iam_auth_token' in sys.argv[2]\n"
        "else:\n"
        f"    sys.exit(subprocess.call([{sys.executable!r}, *sys.argv[1:]]))\n"
    )
    dispatcher.chmod(0o700)
    capture = tmp_path / "capture.py"
    output = tmp_path / "observed.json"
    capture.write_text(
        "import json, os\nfrom pathlib import Path\n"
        f"Path({str(output)!r}).write_text(json.dumps({{k: os.environ.get(k) for k in "
        "['DB_AUTH_MODE', 'DB_IAM_AUTH', 'DB_USER', 'DB_PASSWORD', 'DB_SECRET_ID']}))\n"
    )
    env = {
        "PATH": str(binary) + os.pathsep + os.defpath,
        "CLOUD_PROVIDER": "aws",
        "DB_AUTH_MODE": "rds-iam",
        "DB_HOST": "db.example.internal",
        "DB_PORT": "5432",
        "DB_NAME": "shifter",
        "DB_USER": "portal_web",
        "DB_SSLMODE": "verify-full",
        "DB_SSL_ROOT_CERT": "/run/rds-ca/ca.pem",
        "SKIP_MIGRATIONS": "1",
        "SHIFTER_IDENTITY_MAINTENANCE": "true",
    }
    env.update(overrides)
    result = subprocess.run(  # noqa: S603 -- fixed repository entrypoint with local synthetic process fixtures
        ["/bin/bash", str(ENTRYPOINT), sys.executable, str(capture)],
        env={key: value for key, value in env.items() if value is not None},
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    return result, output


def test_aws_runtime_connects_with_its_own_iam_login_from_start(tmp_path):
    result, output = _aws_rds_iam_run(tmp_path)
    assert result.returncode == 0, result.stderr
    observed = json.loads(output.read_text())
    assert observed == {
        "DB_AUTH_MODE": "rds-iam",
        "DB_IAM_AUTH": "true",
        "DB_USER": "portal_web",
        "DB_PASSWORD": None,
        "DB_SECRET_ID": None,
    }


def test_aws_runtime_refuses_master_secret_password_or_unverified_tls(tmp_path):
    for overrides in (
        {"DB_SECRET_ID": "arn:aws:secretsmanager:us-east-2:123456789012:secret:shifter/dev/eks/database"},
        {"DB_PASSWORD": "stored-password"},
        {"DB_SSLMODE": "require"},
        {"CLOUD_PROVIDER": "gcp"},
        {"SKIP_MIGRATIONS": None},
    ):
        result, _ = _aws_rds_iam_run(tmp_path, **overrides)
        assert result.returncode != 0


def test_aws_runtime_materializes_the_published_rds_ca_bundle(tmp_path):
    import base64

    pem = "-----BEGIN CERTIFICATE-----\nU3ludGhldGlj\n-----END CERTIFICATE-----\n"
    result, output = _aws_rds_iam_run(
        tmp_path,
        DB_SSL_ROOT_CERT=None,
        DB_SSL_ROOT_CERT_PEM_B64=base64.b64encode(pem.encode()).decode(),
        TMPDIR=str(tmp_path),
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(output.read_text())["DB_IAM_AUTH"] == "true"
    written = list(tmp_path.glob("rds-ca-*.pem"))
    assert len(written) == 1 and written[0].read_text() == pem
    assert oct(written[0].stat().st_mode & 0o777) == "0o444"


def test_password_readiness_probe_verifies_the_server_like_django(tmp_path):
    binary = tmp_path / "bin"
    binary.mkdir()
    probe = tmp_path / "probe.json"
    dispatcher = binary / "python"
    dispatcher.write_text(
        f"#!{sys.executable}\n"
        "import json, subprocess, sys, types\n"
        "if sys.argv[1] == '-c' and 'psycopg.connect(' in sys.argv[2]:\n"
        "    recorded = {}\n"
        "    sys.modules['psycopg'] = types.SimpleNamespace(connect=lambda **kwargs: recorded.update(kwargs))\n"
        "    exec(sys.argv[2], {'__name__': '__main__'})\n"
        f"    open({str(probe)!r}, 'w').write(json.dumps({{k: v for k, v in recorded.items() if k != 'password'}}))\n"
        "else:\n"
        f"    sys.exit(subprocess.call([{sys.executable!r}, *sys.argv[1:]]))\n"
    )
    dispatcher.chmod(0o700)
    env = {
        "PATH": str(binary) + os.pathsep + os.defpath,
        "CLOUD_PROVIDER": "aws",
        "DB_HOST": "db.example.internal",
        "DB_PORT": "5432",
        "DB_NAME": "shifter",
        "DB_USER": "shifter_admin",
        "DB_PASSWORD": "synthetic-master-password",
        "DB_SSLMODE": "verify-full",
        "DB_SSL_ROOT_CERT": "/run/rds-ca/ca.pem",
        "SKIP_MIGRATIONS": "1",
        "SHIFTER_IDENTITY_MAINTENANCE": "true",
    }
    result = subprocess.run(  # noqa: S603 -- fixed repository entrypoint with local synthetic process fixtures
        ["/bin/bash", str(ENTRYPOINT), sys.executable, "-c", "pass"],
        env=env,
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    observed = json.loads(probe.read_text())
    assert (observed["sslmode"], observed["sslrootcert"]) == ("verify-full", "/run/rds-ca/ca.pem")
    assert observed["user"] == "shifter_admin"


def test_owner_shell_job_runs_before_activation_only_in_maintenance(tmp_path):
    binary = tmp_path / "bin"
    binary.mkdir()
    ran = tmp_path / "owner-ran"
    dispatcher = binary / "python"
    dispatcher.write_text(
        f"#!{sys.executable}\n"
        "import subprocess, sys\n"
        "if sys.argv[1:3] == ['manage.py', 'identity_runtime_check']:\n"
        "    sys.exit('identity runtime is not activated')\n"
        "if sys.argv[1] == '-c' and 'psycopg.connect(' in sys.argv[2]:\n"
        "    sys.exit(0)\n"
        f"sys.exit(subprocess.call([{sys.executable!r}, *sys.argv[1:]]))\n"
    )
    dispatcher.chmod(0o700)
    env = {
        "PATH": str(binary) + os.pathsep + os.defpath,
        "CLOUD_PROVIDER": "aws",
        "DB_HOST": "db.example.internal",
        "DB_PORT": "5432",
        "DB_NAME": "shifter",
        "DB_USER": "shifter_admin",
        "DB_PASSWORD": "synthetic-master-password",
        "SKIP_MIGRATIONS": "1",
    }
    owner = ["/bin/sh", "-c", f"touch {ran}"]
    for maintenance, admitted in (("true", True), (None, False)):
        ran.unlink(missing_ok=True)
        run_env = {**env, "SHIFTER_IDENTITY_MAINTENANCE": maintenance} if maintenance else env
        result = subprocess.run(  # noqa: S603 -- fixed repository entrypoint with local synthetic process fixtures
            ["/bin/bash", str(ENTRYPOINT), *owner],
            env=run_env,
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert (result.returncode == 0) is admitted, result.stderr
        assert ran.exists() is admitted
