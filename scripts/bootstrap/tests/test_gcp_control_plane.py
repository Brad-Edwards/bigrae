"""Behavior tests split from deploy.py: test_gcp_control_plane.py.

Tests verify the complete contract for each function:
1. Inputs - minimum required data and validation
2. Outputs - return values and data structures
3. Side effects - subprocess calls, file writes, system changes
4. Errors - error handling and propagation
5. Logging - debug and error logging

All external dependencies are mocked. No actual AWS calls, file operations,
or subprocess executions occur during tests.
"""

import io
import json
import shutil
import subprocess
from pathlib import Path
from typing import ClassVar
from unittest.mock import Mock, patch

import pytest

import deploy
import gcp_control_plane

PINNED_IMAGE_TAG = "abc1234"

# =============================================================================
# Test Fixtures
# =============================================================================


def _sample_gcp_control_plane_outputs(project_id: str = "example-gcp-project") -> dict[str, dict[str, object]]:
    """Return representative Terraform outputs for the GCP control-plane path."""
    return {
        "gke_cluster_name": {"value": "shifter-gcp-dev-platform"},
        "gke_cluster_location": {"value": "us-central1"},
        "artifact_registry_image_roots": {
            "value": {
                "portal": f"us-central1-docker.pkg.dev/{project_id}/shifter-gcp-dev-portal/portal",
                "guacd": f"us-central1-docker.pkg.dev/{project_id}/shifter-gcp-dev-guacd/guacd",
                "guacamole-client": (
                    f"us-central1-docker.pkg.dev/{project_id}/shifter-gcp-dev-guacamole-client/guacamole-client"
                ),
                "pulumi-provisioner": (
                    f"us-central1-docker.pkg.dev/{project_id}/shifter-gcp-dev-pulumi-provisioner/pulumi-provisioner"
                ),
            }
        },
        "attested_image_identities": {
            "value": {
                "platform": (
                    f"us-central1-docker.pkg.dev/{project_id}/shifter-gcp-dev-portal/portal@sha256:" + ("1" * 64)
                ),
                "guacd": (f"us-central1-docker.pkg.dev/{project_id}/shifter-gcp-dev-guacd/guacd@sha256:" + ("2" * 64)),
                "guacamoleClient": (
                    f"us-central1-docker.pkg.dev/{project_id}/shifter-gcp-dev-guacamole-client/"
                    "guacamole-client@sha256:" + ("3" * 64)
                ),
            }
        },
        "assets_bucket_name": {"value": f"{project_id}-gcp-dev-assets"},
        "terraform_state_bucket_name": {"value": f"{project_id}-terraform-state"},
        "platform_events_topic_id": {"value": f"projects/{project_id}/topics/shifter-gcp-dev-events"},
        "platform_event_subscriptions": {
            "value": {
                "cms": f"projects/{project_id}/subscriptions/shifter-gcp-dev-cms",
                "engine": f"projects/{project_id}/subscriptions/shifter-gcp-dev-engine",
                "mc": f"projects/{project_id}/subscriptions/shifter-gcp-dev-mc",
                "experiments": f"projects/{project_id}/subscriptions/shifter-gcp-dev-experiments",
            }
        },
        "runtime_secret_ids": {
            "value": {
                "app": f"projects/{project_id}/secrets/shifter-gcp-dev-app",
                "db": f"projects/{project_id}/secrets/shifter-gcp-dev-db",
                "db-provisioner": f"projects/{project_id}/secrets/shifter-gcp-dev-db-provisioner",
                "db-migration": f"projects/{project_id}/secrets/shifter-gcp-dev-db-migration",
                "guacamole-db": f"projects/{project_id}/secrets/shifter-gcp-dev-guacamole-db",
                "guacamole-json-auth": f"projects/{project_id}/secrets/shifter-gcp-dev-guacamole-json-auth",
                # ADR-008-R6 (#963): the GCP runtime renderer fails closed
                # without the Memorystore Secret Manager bundle ID.
                "redis": f"projects/{project_id}/secrets/shifter-gcp-dev-redis",
            }
        },
        "dynamic_secret_project_id": {"value": f"{project_id}-range-secrets"},
        "provisioner_static_secret_refs": {
            "value": {
                "GDC_VM_IMAGE_GCS_SECRET_ID": (f"projects/{project_id}/secrets/shifter-gcp-dev-gdc-vm-image-gcs"),
            }
        },
        "identity_platform_api_key": {"value": "identity-platform-api-key"},
        "identity_platform_project_id": {"value": project_id},
        "identity_allowed_email_domain": {"value": "paloaltonetworks.com"},
        "identity_allowed_emails": {"value": []},
        "control_plane_database": {
            "value": {
                "private_ip": "10.40.0.10",
                "port": 5432,
                "database_name": "shifter",
                "user_name": "portal_runtime",
                "provisioner_user_name": "provisioner_runtime",
            }
        },
        # ADR-008-R6 (#963): Memorystore runs with TLS on the GCP runtime,
        # so the cache payload must carry tls_enabled or the renderer fails
        # closed.
        "control_plane_cache": {"value": {"host": "10.40.0.20", "port": 6378, "tls_enabled": True}},
        "guacamole_database": {
            "value": {
                "host": "10.40.0.10",
                "port": 5432,
                "database_name": "guacamole",
                "user_name": "guacamole",
            }
        },
        "public_ingress_ip_name": {"value": "shifter-gcp-dev-platform-ip"},
        "public_ingress_ip_address": {"value": "34.123.45.67"},
        "public_hostname": {"value": "portal.example.test"},
        "managed_tls_enabled": {"value": True},
        "cloud_armor_security_policy_name": {"value": "shifter-gcp-dev-edge"},
        "range_network_id": {"value": f"projects/{project_id}/global/networks/shifter-gcp-dev-range"},
        "range_network_cidr": {"value": "10.50.0.0/16"},
        "range_network_region": {"value": "us-central1"},
        "portal_network_cidrs": {"value": ["10.46.0.0/20"]},
        "access_network_cidrs": {"value": ["10.47.0.0/20"]},
        "gke_services_cidr": {"value": "10.48.0.0/20"},
        "gke_master_ipv4_cidr": {"value": "172.16.0.0/28"},
        "workload_service_accounts": {
            "value": {
                "portal": f"shiftergcpdev-portal@{project_id}.iam.gserviceaccount.com",
                "workers": f"shiftergcpdev-workers@{project_id}.iam.gserviceaccount.com",
                "ctf-scheduler": f"shiftergcpdev-ctf-scheduler@{project_id}.iam.gserviceaccount.com",
                "migrator": f"shiftergcpdev-migrator@{project_id}.iam.gserviceaccount.com",
                # account_id is bounded to <=30 chars, so provisioner-launcher's SA
                # localpart is shortened to prov-launcher (#1719).
                "provisioner-launcher": (f"shiftergcpdev-prov-launcher@{project_id}.iam.gserviceaccount.com"),
                "provisioner": f"shiftergcpdev-provisioner@{project_id}.iam.gserviceaccount.com",
            }
        },
    }


_IMAGE_IDENTITIES = {
    "platform": "us-central1-docker.pkg.dev/example-gcp-project/shifter-gcp-dev-portal/portal@sha256:" + ("1" * 64),
    "guacd": "us-central1-docker.pkg.dev/example-gcp-project/shifter-gcp-dev-guacd/guacd@sha256:" + ("2" * 64),
    "guacamoleClient": (
        "us-central1-docker.pkg.dev/example-gcp-project/shifter-gcp-dev-guacamole-client/guacamole-client@sha256:"
        + ("3" * 64)
    ),
}


def _runtime_env(config, outputs) -> dict[str, str]:
    """Compose the runtime env the way gcp_runtime does, with the renderer run in-process."""
    renderer = gcp_control_plane._load_python_script_module(
        Path(__file__).resolve().parents[2] / "gcp" / "render_runtime_env.py", "test_cp_render_runtime_env"
    )
    static = gcp_control_plane.parse_env_contract(gcp_control_plane.render_gcp_platform_runtime_env(config))
    rendered = gcp_control_plane.parse_env_contract(renderer.render_env(outputs, engine_image="sha256:" + "9" * 64))
    return {**static, **rendered}


def _render_values(config, outputs, **kwargs) -> dict[str, object]:
    return deploy.render_gcp_helm_values(
        config, outputs, runtime_env=_runtime_env(config, outputs), image_identities=dict(_IMAGE_IDENTITIES), **kwargs
    )


def _completed(*, returncode: int = 0, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess[str]:
    """Return a typed CompletedProcess test double."""
    return subprocess.CompletedProcess(["cmd"], returncode, stdout=stdout, stderr=stderr)


@pytest.fixture
def mock_stdin_tty():
    """Mock sys.stdin.isatty() to return True (interactive terminal)."""
    with patch("sys.stdin.isatty", return_value=True):
        yield


@pytest.fixture
def mock_stdin_non_tty():
    """Mock sys.stdin.isatty() to return False (non-interactive)."""
    with patch("sys.stdin.isatty", return_value=False):
        yield


@pytest.fixture(autouse=True)
def prevent_hanging_on_input():
    """Automatically mock input() to prevent tests from hanging.

    Individual tests should override this with their specific input values.
    """
    with patch("builtins.input", return_value=""):
        yield


@pytest.fixture(autouse=True)
def prevent_real_subprocess_calls():
    """Prevent any real subprocess calls from executing.

    This is a safety measure to ensure tests NEVER call real AWS, gh, git,
    terraform, or any other system commands. Individual tests must explicitly
    patch subprocess.run/call/check_output with their expected behavior.

    If a test tries to call subprocess without mocking, it will fail with
    a clear error message.
    """

    def safe_run(*args, **kwargs):
        # Allow only if explicitly mocked in test
        raise RuntimeError(
            f"Test attempted to call subprocess.run({args[0] if args else 'unknown'}) "
            f"without mocking! This could execute real commands. "
            f"Mock subprocess.run in your test."
        )

    with patch("subprocess.run", side_effect=safe_run):
        yield


@pytest.fixture
def bootstrap_config(github_identity):
    """Return a valid BootstrapConfig for testing."""
    return deploy.BootstrapConfig(env="dev", **github_identity)


@pytest.fixture
def mock_repo_root(tmp_path):
    """Mock repository structure for testing."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "platform").mkdir()
    (repo / "platform" / "terraform").mkdir()
    (repo / "platform" / "terraform" / "environments").mkdir()
    (repo / "platform" / "terraform" / "environments" / "dev").mkdir()
    (repo / "platform" / "terraform" / "environments" / "dev" / "portal").mkdir(parents=True)
    (repo / "platform" / "terraform" / "environments" / "dev" / "range").mkdir(parents=True)
    (repo / "platform" / "terraform" / "environments" / "prod").mkdir()
    (repo / "platform" / "terraform" / "environments" / "prod" / "portal").mkdir(parents=True)
    (repo / "platform" / "terraform" / "environments" / "prod" / "range").mkdir(parents=True)
    # global/iam for bootstrap_account tests
    (repo / "platform" / "terraform" / "global" / "iam").mkdir(parents=True)
    return repo


@pytest.fixture
def mock_subprocess():
    """Mock subprocess.run to return successful CompletedProcess.

    This overrides the autouse safety fixture for tests that need
    subprocess calls. Returns the mock for assertion purposes.
    """

    def smart_subprocess(cmd, **kwargs):
        # Return appropriate responses based on command
        cmd_str = " ".join(cmd) if isinstance(cmd, list) else str(cmd)

        # which gh - check if gh CLI is available
        if "which" in cmd_str and "gh" in cmd_str:
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="/usr/bin/gh\n", stderr="")
        # AWS OIDC provider list
        elif "list-open-id-connect-providers" in cmd_str:
            return subprocess.CompletedProcess(
                args=cmd,
                returncode=0,
                stdout="arn:aws:iam::123456789012:oidc-provider/token.actions.githubusercontent.com\n",
                stderr="",
            )
        # Terraform output -json
        elif "terraform" in cmd_str and "output" in cmd_str and "-json" in cmd_str:
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout='{"test_output": {"value": "test"}}', stderr=""
            )
        # Terraform show (for displaying plan)
        elif "terraform" in cmd_str and "show" in cmd_str:
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="terraform plan output...", stderr="")
        # Git commands (add, commit, push) or gh secret set
        elif ("git" in cmd_str and any(x in cmd_str for x in ["add", "commit", "push"])) or (
            "gh" in cmd_str and "secret" in cmd_str and "set" in cmd_str
        ):
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")
        # Default success
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

    with patch("subprocess.run", side_effect=smart_subprocess) as mock:
        yield mock


# =============================================================================
# Test: check_dependencies()
# =============================================================================


# =============================================================================
# Split behavior tests
# =============================================================================


class TestGcpControlPlaneSecurityInputs:
    """Tests for the bootstrap security preflight that runs before Terraform apply."""

    def test_reads_security_inputs_from_tfvars(self, tmp_path):
        """Bootstrap should read the hostname, TLS, and admin CIDR inputs from terraform.tfvars."""
        tf_dir = tmp_path / "gcp-dev"
        tf_dir.mkdir()
        (tf_dir / "terraform.tfvars").write_text(
            """
public_hostname = "portal.example.test"
enable_managed_tls = true
gke_master_authorized_cidrs = [
  "198.51.100.10/32",
  "203.0.113.0/24",
]
"""
        )

        settings = deploy.read_gcp_control_plane_security_inputs(tf_dir)

        assert settings == {
            "public_hostname": "portal.example.test",
            "enable_managed_tls": True,
            "gke_master_authorized_cidrs": ["198.51.100.10/32", "203.0.113.0/24"],
        }

    def test_reads_security_inputs_from_auto_tfvars_with_last_assignment_wins(self, tmp_path):
        """Bootstrap should include sorted auto.tfvars so generated security inputs are honored."""
        tf_dir = tmp_path / "gcp-dev"
        tf_dir.mkdir()
        (tf_dir / "terraform.tfvars").write_text(
            """
public_hostname = "portal.example.test"
enable_managed_tls = false
gke_master_authorized_cidrs = ["198.51.100.10/32"]
"""
        )
        (tf_dir / "99-security.auto.tfvars").write_text(
            """
enable_managed_tls = true
gke_master_authorized_cidrs = ["203.0.113.0/24"]
"""
        )

        assert deploy.read_gcp_control_plane_security_inputs(tf_dir) == {
            "public_hostname": "portal.example.test",
            "enable_managed_tls": True,
            "gke_master_authorized_cidrs": ["203.0.113.0/24"],
        }

    def test_validate_security_inputs_rejects_insecure_defaults(self, tmp_path):
        """Bootstrap must fail before Terraform apply when ingress and control-plane access are insecure."""
        tf_dir = tmp_path / "gcp-dev"
        tf_dir.mkdir()
        (tf_dir / "terraform.tfvars").write_text(
            """
public_hostname = ""
enable_managed_tls = false
gke_master_authorized_cidrs = []
"""
        )

        with pytest.raises(ValueError, match="public hostname"):
            deploy.validate_gcp_control_plane_security_inputs(tf_dir)

    def test_validate_security_inputs_rejects_disabled_managed_tls(self, tmp_path):
        """A valid hostname must not hide an explicitly disabled managed TLS setting."""
        tf_dir = tmp_path / "gcp-dev"
        tf_dir.mkdir()
        (tf_dir / "terraform.tfvars").write_text(
            'public_hostname = "portal.example.test"\n'
            "enable_managed_tls = false\n"
            'gke_master_authorized_cidrs = ["10.42.0.0/16"]\n'
        )

        with pytest.raises(ValueError, match="managed TLS"):
            deploy.validate_gcp_control_plane_security_inputs(tf_dir)

    @staticmethod
    def _write_secure_tfvars(tf_dir, cidrs):
        """Write a terraform.tfvars whose hostname/TLS pass, so a test isolates the CIDR allowlist check."""
        tf_dir.mkdir()
        cidr_lines = "".join(f'  "{cidr}",\n' for cidr in cidrs)
        (tf_dir / "terraform.tfvars").write_text(
            'public_hostname = "portal.example.test"\n'
            "enable_managed_tls = true\n"
            f"gke_master_authorized_cidrs = [\n{cidr_lines}]\n"
        )

    @pytest.mark.parametrize(
        ("cidrs", "expected_match"),
        [
            # The same contract the Terraform `validation` block on
            # `gke_master_authorized_cidrs` enforces — keep these in lockstep.
            (["0.0.0.0/0"], r"/0 range"),
            (["::/0"], r"/0 range"),
            (["10.42.0.0/16", "0.0.0.0/0"], r"/0 range"),
            (["203.0.113.10"], r"explicit /N prefix"),
            (["not-a-cidr"], r"explicit /N prefix"),
            (["not/a/cidr"], r"not a valid CIDR"),
            (["198.51.100.999/32"], r"not a valid CIDR"),
            (["198.51.100.0/33"], r"not a valid CIDR"),
            (["198.51.100.10/32"], r"only RFC1918"),
            (["2001:db8::/48"], r"only RFC1918"),
        ],
        ids=[
            "ipv4_world_open",
            "ipv6_world_open",
            "mixed_with_world_open",
            "bare_ip_no_prefix",
            "no_prefix_garbage",
            "garbage_with_slashes",
            "bad_octet",
            "bad_prefix",
            "public_ipv4",
            "ipv6",
        ],
    )
    def test_validate_security_inputs_rejects_unsafe_authorized_cidrs(self, tmp_path, cidrs, expected_match):
        """Bootstrap must reject malformed CIDR entries and world-open /0 ranges in the admin allowlist."""
        tf_dir = tmp_path / "gcp-dev"
        self._write_secure_tfvars(tf_dir, cidrs)

        with pytest.raises(ValueError, match=expected_match):
            deploy.validate_gcp_control_plane_security_inputs(tf_dir)

    def test_validate_security_inputs_accepts_specific_private_cidrs(self, tmp_path):
        """A non-empty allowlist of specific RFC1918 CIDRs passes the preflight.

        The validator is a side-effect-only contract (raise on bad input, return
        None on good input), so the assertions cover both halves: the documented
        None return and a round-trip parse that proves the test fixture's input
        was actually consumed (so a future refactor that silently skipped the
        CIDR loop would still be caught here, not just by the negative cases).
        """
        tf_dir = tmp_path / "gcp-dev"
        cidrs = ["10.42.0.0/16", "172.20.1.0/24", "192.168.50.10/32"]
        self._write_secure_tfvars(tf_dir, cidrs)

        assert deploy.validate_gcp_control_plane_security_inputs(tf_dir) is None
        assert deploy.read_gcp_control_plane_security_inputs(tf_dir)["gke_master_authorized_cidrs"] == cidrs


class TestGdcControlPlaneHelmValues:
    """Tests for rendering Helm values for the GCP Shifter release."""

    def test_renders_values_with_live_project_specific_inputs(self):
        """The generated values must carry project-specific images, env contracts, and identity bindings."""
        config = deploy.GDCBootstrapConfig(project_id="example-gcp-project", cluster_id="cluster1")
        outputs = _sample_gcp_control_plane_outputs(config.project_id)
        values = _render_values(config, outputs)

        assert values["releaseNamespace"] == "shifter-system"
        # CLOUD_PROVIDER reaches the merged runtime env solely via the GCP backend
        # runtime-env renderer (scripts/gcp/render_runtime_env.py); PLAT-2005.
        assert values["runtimeEnv"]["CLOUD_PROVIDER"] == "gcp"
        assert values["runtimeEnv"]["GCP_PROJECT_ID"] == "example-gcp-project"
        assert values["runtimeEnv"]["GOOGLE_CLOUD_PROJECT"] == "example-gcp-project"
        assert values["runtimeEnv"]["DJANGO_DEBUG"] == "false"
        assert values["runtimeEnv"]["SESSION_COOKIE_SECURE"] == "true"
        assert values["runtimeEnv"]["SITE_URL"] == "https://portal.example.test"
        assert (
            values["runtimeEnv"]["GDC_VM_IMAGE_GCS_SECRET_ID"]
            == "projects/example-gcp-project/secrets/shifter-gcp-dev-gdc-vm-image-gcs"
        )
        assert (
            values["serviceAccounts"]["portal"]["annotations"]["iam.gke.io/gcp-service-account"]
            == "shiftergcpdev-portal@example-gcp-project.iam.gserviceaccount.com"
        )
        assert (
            values["serviceAccounts"]["workers"]["annotations"]["iam.gke.io/gcp-service-account"]
            == "shiftergcpdev-workers@example-gcp-project.iam.gserviceaccount.com"
        )
        assert (
            values["serviceAccounts"]["provisioner"]["annotations"]["iam.gke.io/gcp-service-account"]
            == "shiftergcpdev-provisioner@example-gcp-project.iam.gserviceaccount.com"
        )

    def test_feature_artifact_acquisition_follows_the_acquirer_identity(self):
        config = deploy.GDCBootstrapConfig(project_id="prod-rwctxzl6shxk", cluster_id="cluster1")
        outputs = _sample_gcp_control_plane_outputs(config.project_id)

        disabled = _render_values(config, outputs)
        assert disabled["capabilities"]["featureArtifactAcquisition"] is False
        assert disabled["featureArtifactAcquisition"]["egressCidrs"] == []
        assert "FEATURE_ARTIFACT_JOB_IMAGE" not in disabled["runtimeEnv"]

        acquirer = f"shiftergcpdev-artifact-acq@{config.project_id}.iam.gserviceaccount.com"
        outputs["workload_service_accounts"]["value"]["artifact-acquirer"] = acquirer
        enabled = _render_values(config, outputs)

        assert enabled["capabilities"]["featureArtifactAcquisition"] is True
        # The Job runs exactly the attested platform image its admission policy pins.
        assert enabled["runtimeEnv"]["FEATURE_ARTIFACT_JOB_IMAGE"] == enabled["images"]["platform"]
        acquisition = enabled["featureArtifactAcquisition"]
        assert acquisition["serviceAccountAnnotations"] == {"iam.gke.io/gcp-service-account": acquirer}
        assert acquisition["egressCidrs"] == ["0.0.0.0/0"]
        assert {"10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16"} <= set(acquisition["egressExcept"])

    def test_migration_job_uses_the_dedicated_identity_and_owner_secret(self):
        config = deploy.GDCBootstrapConfig(project_id="example-gcp-project", cluster_id="cluster1")
        outputs = _sample_gcp_control_plane_outputs(config.project_id)
        values = _render_values(config, outputs)

        prerequisites, job = deploy.render_gcp_migration_manifests(outputs, values)

        service_account, runtime_config = prerequisites["items"]
        assert service_account["metadata"]["name"] == "migrator"
        assert (
            service_account["metadata"]["annotations"]["iam.gke.io/gcp-service-account"]
            == outputs["workload_service_accounts"]["value"]["migrator"]
        )
        assert runtime_config["data"]["DB_SECRET_ID"].endswith("-db")
        assert runtime_config["data"]["DB_MIGRATION_SECRET_ID"].endswith("-db-migration")
        pod = job["spec"]["template"]["spec"]
        assert pod["serviceAccountName"] == "migrator"
        container = pod["containers"][0]
        # Entrypoint migrates first; then register the shipped catalog and base
        # image mappings through the same idempotent commands used by deploy.
        assert container["args"] == [
            "/bin/sh",
            "-c",
            "python manage.py reconcile_gcp_database_duties"
            " && python manage.py bootstrap_inbox_catalog && python manage.py seed_raes_image_registry"
            " && python manage.py acquire_feature_artifacts",
        ]
        db_secret = next(item for item in container["env"] if item["name"] == "DB_SECRET_ID")
        assert db_secret["valueFrom"]["configMapKeyRef"]["key"] == "DB_MIGRATION_SECRET_ID"
        temp_dir = next(item for item in container["env"] if item["name"] == "TMPDIR")["value"]
        assert temp_dir == "/var/run/shifter-migrate"

    def test_native_migration_keeps_owner_auth_and_a_managed_native_connector(self):
        config = deploy.GDCBootstrapConfig(project_id="example-gcp-project", cluster_id="cluster1")
        outputs = _sample_gcp_control_plane_outputs(config.project_id)
        values = _render_values(config, outputs)
        values["cloudSqlProxy"] = {
            "enabled": True,
            "instanceConnectionName": "example-gcp-project:us-central1:platform-pg",
            "databaseCidrs": ["10.60.0.12/32"],
        }
        prerequisites, job = deploy.render_gcp_migration_manifests(outputs, values)
        network = next(item for item in prerequisites["items"] if item["kind"] == "NetworkPolicy")
        assert network["spec"]["podSelector"] == {"matchLabels": {"app.kubernetes.io/component": "migrator"}}
        from ipaddress import ip_address, ip_network

        def allowed(address, port):
            return any(
                any(item["port"] == port for item in rule["ports"])
                and any(
                    "ipBlock" in peer and ip_address(address) in ip_network(peer["ipBlock"]["cidr"])
                    for peer in rule["to"]
                )
                for rule in network["spec"]["egress"]
            )

        assert allowed("10.60.0.12", 3307) and not allowed("10.60.0.13", 3307)
        assert allowed("199.36.153.8", 443) and not allowed("203.0.113.10", 443)
        assert allowed("169.254.169.254", 80) and allowed("169.254.169.254", 988)
        dns = [rule for rule in network["spec"]["egress"] if any(p["port"] == 53 for p in rule["ports"])]
        assert dns and dns[0]["to"] == [
            {
                "namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "kube-system"}},
                "podSelector": {"matchLabels": {"k8s-app": "kube-dns"}},
            }
        ]
        pod = job["spec"]["template"]["spec"]
        proxy = pod["initContainers"][0]
        assert proxy["restartPolicy"] == "Always"
        assert "--auto-iam-authn" not in proxy["args"]
        assert "--address=127.0.0.1" in proxy["args"]
        assert "--run-connection-test" in proxy["args"]
        environment = {item["name"]: item for item in pod["containers"][0]["env"]}
        assert environment["DB_AUTH_MODE"]["value"] == "password"
        assert environment["DB_HOST"]["value"] == "127.0.0.1"
        assert "reconcile_gcp_database_duties" in pod["containers"][0]["args"][-1]
        container = pod["containers"][0]
        temp_dir = next(item for item in container["env"] if item["name"] == "TMPDIR")["value"]
        assert container["volumeMounts"] == [{"name": "tmp", "mountPath": temp_dir}]
        # The portal image's USER is the name appuser (uid 10001); pair
        # runAsNonRoot with the numeric uid so the kubelet can verify non-root
        # (gcp-deploy runs this migrate Job for CI and operators; #2244, #2571).
        assert container["securityContext"]["runAsNonRoot"] is True
        assert container["securityContext"]["runAsUser"] == 10001
        # DB-only Job: REDIS_SECRET_ID is blank (no REDIS_PASSWORD hydration), so
        # REDIS_HOST must also be blank or Django fails closed at import (#2245).
        redis_secret = next(item for item in container["env"] if item["name"] == "REDIS_SECRET_ID")
        assert redis_secret["value"] == ""
        redis_host = next(item for item in container["env"] if item["name"] == "REDIS_HOST")
        assert redis_host["value"] == ""
        from shared.model_access.runtime import load_mounted_catalog

        effective = {
            "MODEL_ACCESS_ENABLED": "true",
            "MODEL_ACCESS_CATALOG_PATH": "/unmounted/catalog.json",
            "MODEL_ACCESS_CATALOG_DIGEST": "sha256:" + "a" * 64,
            **{item["name"]: item["value"] for item in container["env"] if "value" in item},
        }
        assert (
            load_mounted_catalog(
                enabled=effective["MODEL_ACCESS_ENABLED"] == "true",
                path=effective["MODEL_ACCESS_CATALOG_PATH"],
                expected_digest=effective["MODEL_ACCESS_CATALOG_DIGEST"],
            )
            is None
        )
        assert container["image"] == values["images"]["platform"]
        assert (
            values["serviceAccounts"]["ctfScheduler"]["annotations"]["iam.gke.io/gcp-service-account"]
            == "shiftergcpdev-ctf-scheduler@example-gcp-project.iam.gserviceaccount.com"
        )

    def test_native_helm_projection_uses_one_identity_and_datastore_readback(self):
        config = deploy.GDCBootstrapConfig(project_id="example-gcp-project", cluster_id="cluster1")
        outputs = _sample_gcp_control_plane_outputs(config.project_id)
        roles = (
            "portal",
            "workers",
            "ctf-scheduler",
            "provisioner-launcher",
            "provisioner",
            "openfga-runtime",
            "openfga-admin",
            "openfga-migrator",
        )
        users = {role: f"synthetic-{role}@{config.project_id}.iam" for role in roles}
        outputs["workload_service_accounts"]["value"].update(
            {role: user + ".gserviceaccount.com" for role, user in users.items()}
        )
        outputs["workload_service_accounts"]["value"]["guacamole-client"] = (
            f"synthetic-guac@{config.project_id}.iam.gserviceaccount.com"
        )
        outputs["native_identity"] = {
            "value": {
                "enabled": True,
                "openfga_enabled": True,
                "instance_connection_name": f"{config.project_id}:us-central1:platform-pg",
                "database_users": users,
            }
        }
        values = _render_values(
            config,
            outputs,
            render_artifacts=gcp_control_plane.GcpRenderArtifacts(
                openfga_settings={
                    "enabled": True,
                    "runtime_secret_ref": f"projects/{config.project_id}/secrets/policy-runtime",
                    "admin_secret_ref": f"projects/{config.project_id}/secrets/policy-admin",
                    "ca_secret_ref": f"projects/{config.project_id}/secrets/policy-ca",
                }
            ),
        )
        assert values["cloudSqlProxy"]["databaseUsers"] == users
        assert values["cloudSqlProxy"]["databaseCidrs"] == ["10.40.0.10/32"]
        assert values["openfga"]["runtimeDatabaseUser"] == users["openfga-runtime"]
        assert (
            values["openfga"]["serviceAccountAnnotations"]["migrator"]["iam.gke.io/gcp-service-account"]
            == users["openfga-migrator"] + ".gserviceaccount.com"
        )
        assert "policy-runtime" not in json.dumps(values)
        assert values["serviceAccounts"]["guacamoleClient"]["annotations"]["iam.gke.io/gcp-service-account"].startswith(
            "synthetic-guac@"
        )
        assert values["images"]["platform"].endswith("@sha256:" + ("1" * 64))
        assert values["images"]["guacd"].endswith("@sha256:" + ("2" * 64))
        assert values["images"]["guacamoleClient"].endswith("@sha256:" + ("3" * 64))
        assert (
            values["runtimeEnv"]["ENGINE_TASK_IMAGE"] == "us-central1-docker.pkg.dev/example-gcp-project/"
            "shifter-gcp-dev-pulumi-provisioner/pulumi-provisioner@sha256:" + "9" * 64
        )
        assert values["guacamoleRuntimeSecret"] == {"name": "guacamole-runtime"}
        assert values["services"]["portal"]["backendConfig"]["securityPolicyName"] == "shifter-gcp-dev-edge"
        assert values["services"]["guacamoleClient"]["backendConfig"]["enabled"] is True
        assert values["services"]["guacamoleClient"]["backendConfig"]["name"] == "guacamole-client"
        assert values["services"]["guacamoleClient"]["backendConfig"]["securityPolicyName"] == "shifter-gcp-dev-edge"
        assert values["network"] == {
            "enabled": True,
            "ingressSourceCidrs": [
                "35.191.0.0/16",  # NOSONAR - Google Cloud Load Balancer range.
                "130.211.0.0/22",  # NOSONAR - Google Cloud Load Balancer range.
            ],
            "providerApiCidrs": [
                "199.36.153.4/30",  # NOSONAR - restricted.googleapis.com VIP.
                "199.36.153.8/30",  # NOSONAR - private.googleapis.com VIP.
            ],
            "privateServiceCidrs": ["10.40.0.10/32", "10.40.0.20/32"],
            "kubernetesApiCidrs": ["10.48.0.0/20", "172.16.0.0/28"],
            "rangeClusterApiCidrs": [],
            "rangeClusterApiPort": 6444,
            "rangeAccessCidrs": ["10.50.0.0/16"],
            "rangeAccessPorts": [22, 3389],
        }

    def test_forwards_mission_control_lease_policy_from_root_config(self):
        """A configured lease policy reaches the runtime env from root config, without
        pre-seeding this render process's environment (issue #27)."""
        import json

        from installation.render import render_mission_control_lease_env
        from installation.schema import RootConfig

        config = deploy.GDCBootstrapConfig(project_id="example-gcp-project", cluster_id="cluster1")
        outputs = _sample_gcp_control_plane_outputs(config.project_id)
        root = RootConfig.model_validate(
            {
                "backend": "gcp",
                "deployment": {"name": "shifter", "domain": "portal.example.test"},
                "secrets": {"django_secret_key": "prompt"},
                "settings": {
                    "mission_control_leases": {
                        "initial_days": 7,
                        "extension_days": 3,
                        "maximum_days": 90,
                        "extensions_enabled": False,
                    }
                },
            }
        )
        values = _render_values(
            config,
            outputs,
            render_artifacts=deploy.GcpRenderArtifacts(
                mission_control_lease_env=render_mission_control_lease_env(root)
            ),
        )
        assert json.loads(values["runtimeEnv"]["MISSION_CONTROL_LEASE_POLICY_JSON"]) == {
            "initial_days": 7,
            "extension_days": 3,
            "maximum_days": 90,
            "extensions_enabled": False,
        }

    def test_range_cluster_api_cidrs_from_control_plane_endpoint(self):
        """The range-cluster egress allowlist mirrors the configured control-plane endpoint."""
        config = deploy.GDCBootstrapConfig(
            project_id="example-gcp-project",
            cluster_id="cluster1",
            control_plane_platform_endpoint="10.240.0.5:6444",
        )
        outputs = _sample_gcp_control_plane_outputs(config.project_id)
        values = _render_values(config, outputs)

        assert values["network"]["rangeClusterApiCidrs"] == ["10.240.0.5/32"]
        assert values["network"]["rangeClusterApiPort"] == 6444

    def test_range_access_cidrs_from_range_network_cidr(self):
        """Participant range access (issue #1349): the portal/guacd egress allowlist
        is the range network CIDR so those workloads can dial range guests."""
        config = deploy.GDCBootstrapConfig(project_id="example-gcp-project", cluster_id="cluster1")
        outputs = _sample_gcp_control_plane_outputs(config.project_id)
        values = _render_values(config, outputs)

        assert values["network"]["rangeAccessCidrs"] == ["10.50.0.0/16"]

    def test_range_access_cidrs_empty_when_range_network_cidr_absent(self):
        """No range network CIDR -> empty allowlist so the egress policy stays unrendered."""
        config = deploy.GDCBootstrapConfig(project_id="example-gcp-project", cluster_id="cluster1")
        outputs = _sample_gcp_control_plane_outputs(config.project_id)
        outputs["range_network_cidr"] = {"value": ""}
        values = _render_values(config, outputs)

        assert values["network"]["rangeAccessCidrs"] == []

    def test_rejects_insecure_public_bootstrap_values(self):
        """The Helm values renderer must refuse public bare-IP debug deployments on GCP."""
        config = deploy.GDCBootstrapConfig(project_id="example-gcp-project", cluster_id="cluster1")
        outputs = _sample_gcp_control_plane_outputs(config.project_id)
        outputs["public_hostname"] = {"value": ""}
        outputs["managed_tls_enabled"] = {"value": False}

        with pytest.raises(ValueError, match="public_hostname"):
            _render_values(config, outputs)

    def test_rejects_mutable_image_identities(self):
        """Chart workloads run only exact release digests (#2571, ADR-037-R6)."""
        config = deploy.GDCBootstrapConfig(project_id="example-gcp-project", cluster_id="cluster1")
        outputs = _sample_gcp_control_plane_outputs(config.project_id)
        identities = dict(_IMAGE_IDENTITIES, platform="registry/portal:latest")
        with pytest.raises(ValueError, match="repository@sha256"):
            deploy.render_gcp_helm_values(
                config, outputs, runtime_env=_runtime_env(config, outputs), image_identities=identities
            )

    def test_generated_values_carry_no_guacamole_secret_payload(self):
        """Generated Helm values reference the runtime Secret by name only (issue #1180).

        Guacamole DB and JSON-auth secrets live in Secret Manager and the
        out-of-band Kubernetes Secret; they must never be serialized into Helm
        values (and thus Helm release history).
        """
        config = deploy.GDCBootstrapConfig(project_id="example-gcp-project", cluster_id="cluster1")
        outputs = _sample_gcp_control_plane_outputs(config.project_id)

        values = _render_values(config, outputs)

        assert values["guacamoleRuntimeSecret"] == {"name": "guacamole-runtime"}
        serialized = json.dumps(values)
        assert "POSTGRESQL_PASSWORD" not in serialized
        assert "JSON_SECRET_KEY" not in serialized
        assert "stringData" not in serialized


class TestGdcControlPlaneHelmChart:
    """Tests for the Helm chart that packages the GCP Shifter deployment."""

    def test_chart_renders_restricted_security_contexts_and_numeric_runtime_ids(self, tmp_path):
        """The chart must render restricted-compatible workloads with pinned runtime IDs."""
        helm = shutil.which("helm")
        assert helm is not None, "helm is required for security-relevant chart render validation"

        config = deploy.GDCBootstrapConfig(project_id="example-gcp-project", cluster_id="cluster1")
        outputs = _sample_gcp_control_plane_outputs(config.project_id)
        values_path = tmp_path / "values.json"
        values_path.write_text(json.dumps(_render_values(config, outputs)))
        chart_dir = Path(__file__).resolve().parents[3] / "platform" / "charts" / "shifter"

        rendered = subprocess.Popen(  # nosec B603 B607
            [
                helm,
                "template",
                "shifter",
                str(chart_dir),
                "--namespace",
                "shifter-system",
                "--values",
                str(chart_dir / "values-gcp-dev.yaml"),
                "--values",
                str(values_path),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        stdout, stderr = rendered.communicate()

        assert rendered.returncode == 0, stderr
        output = stdout
        assert "kind: Deployment" in output
        assert "name: portal-web" in output
        assert "name: worker-cms" in output
        assert "name: guacd" in output
        assert "name: guacamole-client" in output
        assert "type: RuntimeDefault" in output
        assert "allowPrivilegeEscalation: false" in output
        assert "runAsNonRoot: true" in output
        assert "runAsUser: 10001" in output
        assert "runAsGroup: 10001" in output
        import yaml

        namespaces = [doc for doc in yaml.safe_load_all(output) if doc and doc["kind"] == "Namespace"]
        assert [doc["metadata"]["name"] for doc in namespaces] == ["shifter-plugins"]
        assert namespaces[0]["metadata"]["labels"]["pod-security.kubernetes.io/enforce"] == "restricted"
        assert "kind: BackendConfig" in output
        assert "kind: NetworkPolicy" in output
        assert "name: default-deny-platform" in output
        assert "name: default-deny-jobs" in output
        assert "199.36.153.4/30" in output
        assert "10.40.0.10/32" in output
        # Participant/operator range access egress (issue #1349): the Helm path
        # must render the policy from network.rangeAccessCidrs (fed by the
        # range_network_cidr Terraform output), scoped to the participant channel
        # ports -- asserted at the rendered-manifest level so values->template
        # wiring drift is caught, mirroring the Kustomize renderer's own test.
        assert "name: allow-platform-range-access-egress" in output
        assert "10.50.0.0/16" in output  # range_network_cidr from the sample outputs
        assert "port: 22" in output
        assert "port: 3389" in output
        assert 'requestPath: "/health/"' in output
        assert "securityPolicy:" in output
        assert "name: shifter-gcp-dev-edge" in output
        assert 'cloud.google.com/backend-config: "{\\"default\\":\\"portal-web\\"}"' in output
        assert 'cloud.google.com/backend-config: "{\\"default\\":\\"guacamole-client\\"}"' in output
        # The chart references the guacamole-runtime Secret; it must never create one (#1180).
        assert "kind: Secret" not in output

    def test_chart_never_renders_a_secret_even_when_values_carry_a_payload(self, tmp_path):
        """Defense in depth (#1180): the chart owns no Secret payload.

        Even if a caller injects secret material under guacamoleRuntimeSecret,
        the chart must not render a Kubernetes Secret or leak the values into
        rendered output / Helm release history. The runtime Secret is created
        out of band by the deploy bootstrap and consumed by reference only.
        """
        helm = shutil.which("helm")
        assert helm is not None, "helm is required for security-relevant chart render validation"

        # Adversarial override: a caller tries to smuggle secrets through values.
        values = {
            "guacamoleRuntimeSecret": {
                "enabled": True,
                "name": "guacamole-runtime",
                "stringData": {
                    "POSTGRESQL_PASSWORD": "should-not-render",
                    "JSON_SECRET_KEY": "should-not-render",
                },
            }
        }
        values_path = tmp_path / "values.json"
        values_path.write_text(json.dumps(values))
        chart_dir = Path(__file__).resolve().parents[3] / "platform" / "charts" / "shifter"

        rendered = subprocess.Popen(  # nosec B603 B607
            [
                helm,
                "template",
                "shifter",
                str(chart_dir),
                "--namespace",
                "shifter-system",
                "--values",
                str(chart_dir / "values-gcp-dev.yaml"),
                "--values",
                str(values_path),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        stdout, stderr = rendered.communicate()

        assert rendered.returncode != 0
        assert "kind: Secret" not in stdout
        assert "should-not-render" not in stdout
        assert "should-not-render" not in stderr
        assert "POSTGRESQL_PASSWORD" not in stdout
        assert "JSON_SECRET_KEY" not in stdout


class TestGdcControlPlaneGuacamoleRuntimeSecret:
    """Tests for syncing guacamole-client runtime credentials outside Helm values."""

    def test_renders_guacamole_runtime_secret_manifest(self):
        manifest = deploy.render_gcp_guacamole_runtime_secret_manifest(
            {"username": "guac", "password": "supersecret"},
            "json-auth-key",
        )

        assert manifest == {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {
                "name": "guacamole-runtime",
                "namespace": "shifter-platform",
                "labels": {"app.kubernetes.io/part-of": "shifter"},
            },
            "type": "Opaque",
            "stringData": {
                "POSTGRESQL_USER": "guac",
                "POSTGRESQL_PASSWORD": "supersecret",
                "JSON_SECRET_KEY": "json-auth-key",
            },
        }

    def test_syncs_guacamole_runtime_secret_through_kubectl_stdin(self):
        config = deploy.GDCBootstrapConfig(project_id="example-gcp-project", cluster_id="cluster1")
        outputs = _sample_gcp_control_plane_outputs(config.project_id)

        def subprocess_run(cmd, **kwargs):
            if cmd[:4] == ["gcloud", "secrets", "versions", "access"]:
                secret_name = cmd[cmd.index("--secret") + 1]
                if secret_name == "shifter-gcp-dev-guacamole-db":
                    return subprocess.CompletedProcess(cmd, 0, stdout='{"username":"guac","password":"supersecret"}')
                if secret_name == "shifter-gcp-dev-guacamole-json-auth":
                    return subprocess.CompletedProcess(cmd, 0, stdout="json-auth-key\n")
            if cmd == ["kubectl", "apply", "-f", "-"]:
                return subprocess.CompletedProcess(cmd, 0, stdout="secret/guacamole-runtime configured\n", stderr="")
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr=f"unexpected command: {cmd}")

        with patch("deploy.subprocess.run", side_effect=subprocess_run) as mock_run:
            changed = deploy.sync_gcp_guacamole_runtime_secret(config, outputs)

        assert changed is True
        calls = [call.args[0] for call in mock_run.call_args_list]
        assert calls[0][:4] == ["gcloud", "secrets", "versions", "access"]
        assert calls[0][calls[0].index("--secret") + 1] == "shifter-gcp-dev-guacamole-db"
        assert calls[1][:4] == ["gcloud", "secrets", "versions", "access"]
        assert calls[1][calls[1].index("--secret") + 1] == "shifter-gcp-dev-guacamole-json-auth"
        assert calls[2] == ["kubectl", "apply", "-f", "-"]
        assert "supersecret" not in " ".join(calls[2])
        applied = json.loads(mock_run.call_args_list[2].kwargs["input"])
        assert applied["metadata"]["name"] == "guacamole-runtime"
        assert applied["stringData"]["POSTGRESQL_PASSWORD"] == "supersecret"

    def test_dry_run_does_not_fetch_or_apply_guacamole_runtime_secret(self):
        config = deploy.GDCBootstrapConfig(project_id="example-gcp-project", cluster_id="cluster1")
        outputs = _sample_gcp_control_plane_outputs(config.project_id)

        with patch("deploy.subprocess.run") as mock_run:
            changed = deploy.sync_gcp_guacamole_runtime_secret(config, outputs, dry_run=True)

        assert changed is False
        mock_run.assert_not_called()


class TestGdcControlPlaneNamespaces:
    """Tests for namespace lifecycle outside the Helm release."""

    def test_creates_required_namespaces_with_restricted_labels(self):
        """Bootstrap must create Helm target namespaces before the release installs."""
        missing_platform = subprocess.CompletedProcess(
            ["kubectl"],
            1,
            stdout="",
            stderr='Error from server (NotFound): namespaces "shifter-platform" not found',
        )
        active_platform = subprocess.CompletedProcess(
            ["kubectl"],
            0,
            stdout=json.dumps({"status": {"phase": "Active"}}),
            stderr="",
        )
        missing_jobs = subprocess.CompletedProcess(
            ["kubectl"],
            1,
            stdout="",
            stderr='Error from server (NotFound): namespaces "shifter-jobs" not found',
        )
        active_jobs = subprocess.CompletedProcess(
            ["kubectl"],
            0,
            stdout=json.dumps({"status": {"phase": "Active"}}),
            stderr="",
        )

        with patch(
            "deploy.subprocess.run",
            side_effect=[
                missing_platform,
                subprocess.CompletedProcess(["kubectl"], 0, stdout="namespace/shifter-platform created\n", stderr=""),
                active_platform,
                missing_jobs,
                subprocess.CompletedProcess(["kubectl"], 0, stdout="namespace/shifter-jobs created\n", stderr=""),
                active_jobs,
            ],
        ) as mock_subprocess:
            deploy.ensure_gcp_control_plane_namespaces()

        apply_calls = [
            call for call in mock_subprocess.call_args_list if call.args[0][:3] == ["kubectl", "apply", "-f"]
        ]
        assert len(apply_calls) == 2
        platform_manifest = json.loads(apply_calls[0].kwargs["input"])
        jobs_manifest = json.loads(apply_calls[1].kwargs["input"])
        assert platform_manifest["metadata"]["name"] == "shifter-platform"
        assert platform_manifest["metadata"]["labels"]["app.kubernetes.io/part-of"] == "shifter"
        assert platform_manifest["metadata"]["labels"]["pod-security.kubernetes.io/enforce"] == "restricted"
        assert jobs_manifest["metadata"]["name"] == "shifter-jobs"
        assert jobs_manifest["metadata"]["labels"]["shifter.dev/plane"] == "jobs"

    def test_waits_for_terminating_namespace_then_recreates_it(self):
        """A terminating namespace from a failed install must be allowed to clear first."""
        terminating_platform = subprocess.CompletedProcess(
            ["kubectl"],
            0,
            stdout=json.dumps(
                {
                    "metadata": {"deletionTimestamp": "2026-04-10T00:00:00Z"},
                    "status": {"phase": "Terminating"},
                }
            ),
            stderr="",
        )
        missing_platform = subprocess.CompletedProcess(
            ["kubectl"],
            1,
            stdout="",
            stderr='Error from server (NotFound): namespaces "shifter-platform" not found',
        )
        active_platform = subprocess.CompletedProcess(
            ["kubectl"],
            0,
            stdout=json.dumps({"status": {"phase": "Active"}}),
            stderr="",
        )
        active_jobs = subprocess.CompletedProcess(
            ["kubectl"],
            0,
            stdout=json.dumps({"status": {"phase": "Active"}}),
            stderr="",
        )

        with (
            patch(
                "deploy.subprocess.run",
                side_effect=[
                    terminating_platform,
                    missing_platform,
                    subprocess.CompletedProcess(
                        ["kubectl"],
                        0,
                        stdout="namespace/shifter-platform created\n",
                        stderr="",
                    ),
                    active_platform,
                    active_jobs,
                    subprocess.CompletedProcess(
                        ["kubectl"],
                        0,
                        stdout="namespace/shifter-jobs configured\n",
                        stderr="",
                    ),
                    active_jobs,
                ],
            ) as mock_subprocess,
            patch("deploy.time.sleep") as mock_sleep,
        ):
            deploy.ensure_gcp_control_plane_namespaces()

        mock_sleep.assert_not_called()
        apply_calls = [
            call for call in mock_subprocess.call_args_list if call.args[0][:3] == ["kubectl", "apply", "-f"]
        ]
        assert len(apply_calls) == 2


class TestGcpPlatformCoreContracts:
    """Tests for the Terraform platform-core contract that bootstrap depends on."""

    def test_workload_identity_bindings_exist_for_platform_service_accounts(self):
        """Portal, workers, and provisioner KSAs must be able to impersonate their GSAs."""
        module_path = (
            Path(__file__).resolve().parents[3]
            / "platform"
            / "terraform"
            / "gcp"
            / "modules"
            / "portal"
            / "iam"
            / "main.tf"
        )
        module_main = module_path.read_text()

        assert 'resource "google_service_account_iam_member" "workload_identity"' in module_main
        assert 'role               = "roles/iam.workloadIdentityUser"' in module_main
        assert '"serviceAccount:${var.workload_identity_pool}[shifter-platform/portal]"' in module_main
        assert '"serviceAccount:${var.workload_identity_pool}[shifter-platform/workers]"' in module_main
        assert '"serviceAccount:${var.workload_identity_pool}[shifter-jobs/provisioner]"' in module_main

    def test_workers_have_pubsub_publish_and_subscribe_permissions(self):
        """The shared workers service account must publish as well as consume Pub/Sub events."""
        module_path = (
            Path(__file__).resolve().parents[3]
            / "platform"
            / "terraform"
            / "gcp"
            / "modules"
            / "portal"
            / "iam"
            / "main.tf"
        )
        module_main = module_path.read_text()

        workers_section = module_main.split("workers = toset([", 1)[1].split("])", 1)[0]
        assert '"roles/pubsub.publisher"' in workers_section
        assert '"roles/pubsub.subscriber"' in workers_section

    def test_portal_has_only_directory_lifecycle_read_permission(self):
        """Lifecycle rechecks use the exact native permission and only the portal GSA."""
        module_path = (
            Path(__file__).resolve().parents[3]
            / "platform"
            / "terraform"
            / "gcp"
            / "modules"
            / "portal"
            / "iam"
            / "main.tf"
        )
        module_main = module_path.read_text()

        portal_section = module_main.split("portal = toset([", 1)[1].split("])", 1)[0]
        assert '"roles/firebaseauth.viewer"' not in portal_section
        native = (module_path.parent / "native_identity.tf").read_text()
        assert 'permissions = ["firebaseauth.users.get"]' in native
        assert 'member  = "serviceAccount:${google_service_account.workload["portal"].email}"' in native

    def test_identity_platform_self_signup_is_allowed_and_guarded_by_before_create_trigger(self):
        """GCP corporate registration must stay open to eligible users and be gated by a blocking function."""
        module_path = (
            Path(__file__).resolve().parents[3]
            / "platform"
            / "terraform"
            / "gcp"
            / "modules"
            / "portal"
            / "identity-platform"
            / "main.tf"
        )
        module_main = module_path.read_text()

        identity_platform_section = module_main.split('resource "google_identity_platform_config" "platform" {', 1)[1]
        assert "disabled_user_signup   = false" in identity_platform_section
        assert "multi_tenant {" in identity_platform_section
        assert "allow_tenants = false" in identity_platform_section
        assert 'event_type   = "beforeCreate"' in identity_platform_section
        # The blocking function is optional because Domain Restricted Sharing can
        # block its allUsers invoker, so the trigger is count-gated.
        assert (
            "google_cloudfunctions_function.identity_platform_before_create[0].https_trigger_url"
            in identity_platform_section
        )

    def test_cloud_armor_sqli_rule_uses_baseline_sensitivity(self):
        """The edge WAF should run SQLi detection at the PL1 baseline to avoid login false positives."""
        module_path = (
            Path(__file__).resolve().parents[3]
            / "platform"
            / "terraform"
            / "gcp"
            / "modules"
            / "portal"
            / "ingress"
            / "main.tf"
        )
        module_main = module_path.read_text()

        assert "evaluatePreconfiguredWaf('sqli-v33-stable'" in module_main
        assert "'sensitivity': 1" in module_main
        assert "opt_out_rule_ids" not in module_main

    def test_cloud_armor_bypasses_raw_multipart_only_for_tenant_pack_uploads(self):
        """Binary pack archives must not disable SQLi/XSS inspection outside their exact upload route."""
        module_path = (
            Path(__file__).resolve().parents[3]
            / "platform"
            / "terraform"
            / "gcp"
            / "modules"
            / "portal"
            / "ingress"
            / "main.tf"
        )
        module_main = module_path.read_text()

        assert "tenant_pack_upload" in module_main
        assert "request.method == 'POST'" in module_main
        assert "/api/v1/cms/organizations/" in module_main
        assert "/packs/$" in module_main
        assert "multipart/form-data;" in module_main
        assert "evaluatePreconfiguredWaf('sqli-v33-stable'" in module_main
        assert "evaluatePreconfiguredWaf('xss-v33-stable') && !(${local.tenant_pack_upload})" in module_main


class TestGcpBootstrapIdentityPlatform:
    """Tests for Identity Platform bootstrap user sourcing and seeding."""

    def test_ensure_gcp_identity_platform_operator_creates_user(self):
        """Bootstrap must create the first operator via the Identity Platform admin API."""
        config = deploy.GDCBootstrapConfig(project_id="example-gcp-project", cluster_id="cluster1")
        outputs = _sample_gcp_control_plane_outputs(config.project_id)

        with (
            patch("deploy._gcp_identity_admin_request", return_value={"localId": "user-123"}) as mock_request,
        ):
            deploy.ensure_gcp_identity_platform_operator(
                config, outputs, credentials=("analyst@paloaltonetworks.com", "correct-horse-battery-staple")
            )

        mock_request.assert_called_once_with(
            config=config,
            outputs=outputs,
            path=f"/projects/{config.project_id}/accounts",
            payload={
                "email": "analyst@paloaltonetworks.com",
                "password": "correct-horse-battery-staple",
                "displayName": "Shifter Operator",
                "emailVerified": True,
            },
        )

    def test_ensure_gcp_identity_platform_operator_returns_operator_email(self):
        """Bootstrap should return the first operator email so the runtime can elevate that user."""
        config = deploy.GDCBootstrapConfig(project_id="example-gcp-project", cluster_id="cluster1")
        outputs = _sample_gcp_control_plane_outputs(config.project_id)

        with (
            patch("deploy._gcp_identity_admin_request", return_value={"localId": "user-123"}),
        ):
            email = deploy.ensure_gcp_identity_platform_operator(
                config, outputs, credentials=("analyst@paloaltonetworks.com", "correct-horse-battery-staple")
            )

        assert email == "analyst@paloaltonetworks.com"

    def test_ensure_gcp_identity_platform_operator_skips_existing_user(self):
        """Bootstrap should treat an existing operator account as success."""
        config = deploy.GDCBootstrapConfig(project_id="example-gcp-project", cluster_id="cluster1")
        outputs = _sample_gcp_control_plane_outputs(config.project_id)

        with (
            patch("deploy._gcp_identity_admin_request", side_effect=RuntimeError("EMAIL_EXISTS")) as mock_request,
        ):
            result = deploy.ensure_gcp_identity_platform_operator(
                config, outputs, credentials=("analyst@paloaltonetworks.com", "correct-horse-battery-staple")
            )

        mock_request.assert_called_once()
        assert result == "analyst@paloaltonetworks.com"

    def test_ensure_gcp_identity_platform_operator_rejects_non_corporate_email(self):
        """Bootstrap must reject an operator email outside the
        identity_allowed_email_domain Terraform output before touching
        Identity Platform — that domain is the same allow-list the
        Identity Platform beforeCreate hook enforces."""
        config = deploy.GDCBootstrapConfig(project_id="example-gcp-project", cluster_id="cluster1")
        outputs = _sample_gcp_control_plane_outputs(config.project_id)
        # _sample_gcp_control_plane_outputs sets identity_allowed_email_domain
        # to "paloaltonetworks.com"; an email outside that domain must fail.

        with (
            pytest.raises(ValueError, match=r"paloaltonetworks\.com"),
        ):
            deploy.ensure_gcp_identity_platform_operator(
                config, outputs, credentials=("intruder@example.com", "correct-horse-battery-staple")
            )

    def test_ensure_gcp_identity_platform_operator_env_fallback(self, monkeypatch):
        """When no identity_allowed_email_domain output is supplied (e.g., a dry
        run before terraform apply), SHIFTER_GCP_OPERATOR_EMAIL_DOMAIN is the
        fallback enforcement seam."""
        monkeypatch.setenv("SHIFTER_GCP_OPERATOR_EMAIL_DOMAIN", "example.org")
        config = deploy.GDCBootstrapConfig(project_id="example-gcp-project", cluster_id="cluster1")
        outputs = _sample_gcp_control_plane_outputs(config.project_id)
        outputs.pop("identity_allowed_email_domain")  # simulate no terraform output

        with (
            pytest.raises(ValueError, match=r"example\.org"),
        ):
            deploy.ensure_gcp_identity_platform_operator(
                config, outputs, credentials=("intruder@example.com", "correct-horse-battery-staple")
            )

    def test_ensure_gcp_identity_platform_operator_rejects_malformed_email(self):
        """Bootstrap must fail before touching Identity Platform when the operator email is malformed."""
        config = deploy.GDCBootstrapConfig(project_id="example-gcp-project", cluster_id="cluster1")
        outputs = _sample_gcp_control_plane_outputs(config.project_id)

        with (
            pytest.raises(ValueError, match=r"@"),
        ):
            deploy.ensure_gcp_identity_platform_operator(
                config, outputs, credentials=("not-an-email", "correct-horse-battery-staple")
            )

    def test_render_gcp_platform_runtime_env_leaves_elevation_to_the_deployment_settings(self):
        """The static contract holds no operator email; settings.operator_bootstrap projects the lists (#2571)."""
        config = deploy.GDCBootstrapConfig(project_id="example-gcp-project", cluster_id="cluster1")

        rendered = deploy.render_gcp_platform_runtime_env(config)

        assert "PLATFORM_BOOTSTRAP_STAFF_EMAILS=\n" in rendered
        assert "PLATFORM_BOOTSTRAP_SUPERUSER_EMAILS=\n" in rendered
        assert "GCP_RANGE_BACKEND=gce\n" in rendered
        assert "ENGINE_TASK_SERVICE_ACCOUNT_NAME=provisioner\n" in rendered
        # CLOUD_PROVIDER must come from the GCP backend runtime-env renderer only
        # (PLAT-2005): _render_gcp_runtime_env merges this bootstrap-owned contract
        # with scripts/gcp/render_runtime_env.py's output, which is the
        # renderer-owned source of the backend identity. A second hardcoded literal
        # here would be redundant and could silently drift from that value.
        assert "CLOUD_PROVIDER" not in deploy.parse_env_contract(rendered)

    def test_render_gcp_platform_runtime_env_uses_blank_guest_password_samples(self):
        """The generated env contract must not embed sample guest passwords in source-controlled output."""
        config = deploy.GDCBootstrapConfig(project_id="example-gcp-project", cluster_id="cluster1")

        rendered = deploy.render_gcp_platform_runtime_env(config)

        # Issue #762: per-instance guest passwords replace shared env
        # entries. The bootstrap-rendered platform-runtime env file must
        # not advertise these legacy keys, and never the legacy literal.
        assert "GDC_WINDOWS_ADMIN_PASSWORD" not in rendered
        assert "GDC_KALI_PASSWORD" not in rendered
        assert "GDC_UBUNTU_PASSWORD" not in rendered
        assert "CortexSavesTheDay!" not in rendered
        assert "kali:kali" not in rendered
        assert "ubuntu:ubuntu" not in rendered

    def test_render_gcp_platform_runtime_env_leaves_static_secret_refs_to_terraform_outputs(self):
        """Static secret refs come only from the Terraform IAM/runtime declaration map."""
        config = deploy.GDCBootstrapConfig(project_id="example-gcp-project", cluster_id="cluster1")

        rendered = deploy.render_gcp_platform_runtime_env(config)

        assert "GDC_VM_IMAGE_GCS_SECRET_ID" not in rendered
        assert "GDC_VMSERIES_IMAGE_GCS_SECRET_ID" not in rendered
        assert "GDC_VMSERIES_BOOTSTRAP_XML_TEMPLATE_SECRET_ID" not in rendered

    def test_render_gcp_platform_runtime_env_wires_guest_image_urls_from_bucket(self):
        """Guest boot images resolve to the packer-gcp export bucket per environment."""
        config = deploy.GDCBootstrapConfig(
            project_id="example-gcp-project", cluster_id="cluster1", environment="gcp-dev"
        )

        rendered = deploy.render_gcp_platform_runtime_env(config)

        bucket = "shifter-gcp-dev-gdc-vm-images"
        assert f"GDC_UBUNTU_IMAGE_URL=gs://{bucket}/ubuntu.qcow2\n" in rendered
        assert f"GDC_KALI_IMAGE_URL=gs://{bucket}/kali.qcow2\n" in rendered
        assert f"GDC_WINDOWS_IMAGE_URL=gs://{bucket}/windows.qcow2\n" in rendered
        assert f"GDC_DC_IMAGE_URL=gs://{bucket}/dc.qcow2\n" in rendered
        assert "GDC_KALI_DISK_SIZE_GIB=40\n" in rendered
        assert "GDC_UBUNTU_IMAGE_URL=\n" not in rendered

    def test_render_gcp_platform_runtime_env_emits_aws_region_for_provisioner_policy(self):
        """AWS_REGION rides the ConfigMap so the range-Job admission policy passes (#1742).

        Django settings alias AWS_REGION to CLOUD_REGION, so the provisioner-launcher
        always emits a non-empty AWS_REGION even on GCP; restrict-provisioner-jobs then
        requires it to exist in platform-runtime with a matching value.
        """
        config = deploy.GDCBootstrapConfig(
            project_id="example-gcp-project", cluster_id="cluster1", environment="gcp-dev", region="us-central1"
        )

        rendered = deploy.render_gcp_platform_runtime_env(config)

        assert "AWS_REGION=us-central1\n" in rendered
        assert "CLOUD_REGION=us-central1\n" in rendered


class TestArtifactRegistryServiceIdentity:
    """Tests for pre-provisioning the Artifact Registry service identity."""

    def test_dry_run_skips_network_calls(self):
        config = deploy.GDCBootstrapConfig(project_id="example-gcp-project", cluster_id="cluster1")

        with patch("subprocess.run") as mock_run, patch("deploy.urllib_request.urlopen") as mock_urlopen:
            deploy.ensure_gcp_artifact_registry_service_identity(config, dry_run=True)

        mock_run.assert_not_called()
        mock_urlopen.assert_not_called()


class TestProvisionerLauncherDeploymentParity:
    """Launcher identity must stay consistent across Terraform, bootstrap, Helm, and runtime config."""

    def test_guacamole_gateway_has_its_own_workload_identity(self):
        service_accounts = _sample_gcp_control_plane_outputs()["workload_service_accounts"]["value"]
        service_accounts["guacamole-client"] = "gateway@synthetic.iam.gserviceaccount.com"
        values = gcp_control_plane._helm_service_account_values(service_accounts)
        assert (
            values["guacamoleClient"]["annotations"]["iam.gke.io/gcp-service-account"]
            == service_accounts["guacamole-client"]
        )
        assert values["guacamoleClient"]["annotations"] != values["workers"]["annotations"]

    def test_bootstrap_renders_dedicated_launcher_without_provisioner_cloud_identity(self):
        service_accounts = _sample_gcp_control_plane_outputs()["workload_service_accounts"]["value"]

        values = gcp_control_plane._helm_service_account_values(service_accounts)

        assert values["provisionerLauncher"] == {
            "name": "provisioner-launcher",
            "annotations": {
                "iam.gke.io/gcp-service-account": service_accounts["provisioner-launcher"],
            },
        }
        assert values["migrator"] == {
            "name": "migrator",
            "annotations": {
                "iam.gke.io/gcp-service-account": service_accounts["migrator"],
            },
        }
        assert values["provisioner"]["annotations"]["iam.gke.io/gcp-service-account"] == service_accounts["provisioner"]
        assert service_accounts["provisioner-launcher"] != service_accounts["provisioner"]

    def test_runtime_and_terraform_keep_launcher_and_privileged_runtime_distinct(self):
        terraform_iam = (
            deploy.get_repo_root() / "platform" / "terraform" / "gcp" / "modules" / "portal" / "iam" / "main.tf"
        ).read_text(encoding="utf-8")
        assert '"provisioner-launcher"' in terraform_iam
        assert "shifter-platform/provisioner-launcher" in terraform_iam
        assert "provisioner-launcher = toset([])" in terraform_iam
        assert "secret_reader_workloads = toset(" in terraform_iam
        for workload in ("portal", "workers", "ctf-scheduler", "provisioner-launcher"):
            assert f'"{workload}"' in terraform_iam


class TestGcpIdentityAdminApi:
    """Tests for the authenticated Identity Platform bootstrap admin requests."""

    def test_gcp_identity_admin_request_uses_authenticated_project_endpoint_without_api_key(self):
        """Bootstrap must use the authenticated project-scoped admin endpoint without appending a web API key."""

        class _FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self) -> bytes:
                return b'{"localId":"user-123"}'

        config = deploy.GDCBootstrapConfig(project_id="example-gcp-project", cluster_id="cluster1")
        outputs = _sample_gcp_control_plane_outputs(config.project_id)

        with (
            patch("deploy._gcp_identity_access_token", return_value="test-access-token"),
            patch("deploy.urllib_request.urlopen", return_value=_FakeResponse()) as mock_urlopen,
        ):
            result = deploy._gcp_identity_admin_request(
                config=config,
                outputs=outputs,
                path=f"/projects/{config.project_id}/accounts",
                payload={"email": "analyst@paloaltonetworks.com", "password": "correct-horse-battery-staple"},
            )

        assert result == {"localId": "user-123"}
        request = mock_urlopen.call_args.args[0]
        assert request.full_url == (f"https://identitytoolkit.googleapis.com/v1/projects/{config.project_id}/accounts")
        assert request.headers["Authorization"] == "Bearer test-access-token"
        assert request.headers["Content-type"] == "application/json"
        assert request.headers["X-goog-user-project"] == config.project_id

    def test_gcp_identity_admin_request_surfaces_identity_platform_error_messages(self):
        """Bootstrap must surface the actual Identity Platform admin error when the request fails."""

        class _FakeHttpError(deploy.urllib_error.HTTPError):
            def __init__(self) -> None:
                super().__init__(
                    url="https://identitytoolkit.googleapis.com/v1/projects/example-gcp-project/accounts",
                    code=400,
                    msg="Bad Request",
                    hdrs=None,
                    fp=io.BytesIO(b'{"error":{"message":"EMAIL_EXISTS"}}'),
                )

        config = deploy.GDCBootstrapConfig(project_id="example-gcp-project", cluster_id="cluster1")
        outputs = _sample_gcp_control_plane_outputs(config.project_id)

        with (
            patch("deploy._gcp_identity_access_token", return_value="test-access-token"),
            patch("deploy.urllib_request.urlopen", side_effect=_FakeHttpError()),
            pytest.raises(RuntimeError, match="EMAIL_EXISTS"),
        ):
            deploy._gcp_identity_admin_request(
                config=config,
                outputs=outputs,
                path=f"/projects/{config.project_id}/accounts",
                payload={"email": "analyst@paloaltonetworks.com", "password": "correct-horse-battery-staple"},
            )


class TestGdcBootstrapRangeBackend:
    """gdc_bootstrap_cluster gates the ABM/GDC substrate on the range backend (#1716)."""

    def test_gce_backend_is_refused_with_the_gcp_deploy_command(self, capsys):
        """The GKE control plane deploys through gcp-deploy (#2571); gdc-bootstrap builds the substrate only."""
        config = deploy.GDCBootstrapConfig(project_id="example-gcp-project", cluster_id="cluster1", range_backend="gce")
        mock_substrate = Mock()
        with pytest.raises(SystemExit):
            gcp_control_plane.gdc_bootstrap_cluster(
                config, confirmation=lambda _p: True, substrate_builder=mock_substrate
            )
        mock_substrate.assert_not_called()
        assert "gcp-deploy" in capsys.readouterr().out

    def test_gdc_backend_builds_only_the_substrate(self):
        """The gdc backend builds the substrate (including the vm-image secret) and nothing else."""
        config = deploy.GDCBootstrapConfig(project_id="example-gcp-project", cluster_id="cluster1", range_backend="gdc")
        operations: list[str] = []
        result = gcp_control_plane.gdc_bootstrap_cluster(
            config,
            dry_run=False,
            confirmation=lambda _prompt: True,
            substrate_builder=lambda *_a, **_k: operations.append("substrate"),
        )
        assert operations == ["substrate"]
        assert result["workstation"] == config.workstation.name

    def test_aborted_confirmation_changes_nothing(self):
        config = deploy.GDCBootstrapConfig(project_id="example-gcp-project", cluster_id="cluster1", range_backend="gdc")
        mock_substrate = Mock()
        with pytest.raises(SystemExit) as exit_info:
            gcp_control_plane.gdc_bootstrap_cluster(
                config, confirmation=lambda _p: False, substrate_builder=mock_substrate
            )
        assert exit_info.value.code == 0
        mock_substrate.assert_not_called()

    def test_substrate_builder_invokes_each_operation_port(self, tmp_path):
        """The substrate builder owns ordering while side effects stay behind explicit ports."""
        config = deploy.GDCBootstrapConfig(project_id="example-gcp-project", cluster_id="cluster1", range_backend="gdc")
        staged = {
            "ssh_metadata": tmp_path / "ssh-metadata",
            "assets_dir": tmp_path / "assets",
            "service_account_key": tmp_path / "bm-gcr.json",
        }
        operations = gcp_control_plane._GDCSubstrateOperations(
            ensure_apis=Mock(),
            ensure_service_account=Mock(),
            stage_assets=Mock(return_value=staged),
            ensure_network=Mock(),
            ensure_instances=Mock(),
            sync_ssh_metadata=Mock(),
            wait_for_ssh=Mock(),
            upload_assets=Mock(),
            run_workstation_script=Mock(),
            sync_access_secret=Mock(),
            sync_vm_image_secret=Mock(),
        )

        gcp_control_plane._build_gdc_substrate(config, operations=operations)

        operations.ensure_apis.assert_called_once_with(config, dry_run=False)
        operations.ensure_service_account.assert_called_once_with(config, dry_run=False)
        operations.stage_assets.assert_called_once()
        assert operations.wait_for_ssh.call_count == len(config.all_hosts)
        assert operations.run_workstation_script.call_count == 4
        operations.sync_vm_image_secret.assert_called_once_with(
            config,
            staged["service_account_key"],
            dry_run=False,
        )


class TestGceRangePreconditions:
    """gcp-deploy gates the range prerequisites (#1509, #2571)."""

    _FULL_ENV: ClassVar[dict[str, str]] = {
        "GCP_PACKER_BUILD_SERVICE_ACCOUNT": "build@prod-x.iam.gserviceaccount.com",
        "GCP_PACKER_VALIDATE_SERVICE_ACCOUNT": "validate@prod-x.iam.gserviceaccount.com",
        "GCP_RELEASE_SCAN_SERVICE_ACCOUNT": "scan@prod-x.iam.gserviceaccount.com",
        "GCP_DEPLOY_SERVICE_ACCOUNT": "deploy@prod-x.iam.gserviceaccount.com",
        "GCP_DESTROY_SERVICE_ACCOUNT": "destroy@prod-x.iam.gserviceaccount.com",
        "GCP_WORKLOAD_IDENTITY_PROVIDER": "projects/1/locations/global/workloadIdentityPools/p/providers/gh",
        "RANGE_NETWORK_ZONE": "us-central1-a",
        "GCP_RANGE_LINUX_IMAGE": "projects/prod-x/global/images/family/shifter-ubuntu",
        "GCP_RANGE_DC_IMAGE": "projects/prod-x/global/images/family/shifter-dc",
        "GCP_RANGE_HOST_SERVICE_ACCOUNT_EMAIL": "range-host@prod-x.iam.gserviceaccount.com",
    }

    @staticmethod
    def _config():
        return deploy.GDCBootstrapConfig(project_id="prod-x", cluster_id="cluster1", range_backend="gce")

    def test_all_present_passes(self, capsys):
        checked: list[tuple[str, str, str]] = []

        def _exists(project: str, kind: str, name: str) -> bool:
            checked.append((project, kind, name))
            return True

        gcp_control_plane.check_gce_range_preconditions(self._config(), env=dict(self._FULL_ENV), image_exists=_exists)
        assert len(checked) == 2
        assert "GCE range preconditions satisfied" in capsys.readouterr().out

    def test_missing_required_var_fails(self):
        config = self._config()
        env = dict(self._FULL_ENV)
        del env["RANGE_NETWORK_ZONE"]
        with pytest.raises(SystemExit) as exc:
            gcp_control_plane.check_gce_range_preconditions(config, env=env, image_exists=lambda *_: True)
        assert exc.value.code == 1

    def test_missing_image_fails(self):
        config = self._config()
        env = dict(self._FULL_ENV)
        with pytest.raises(SystemExit) as exc:
            gcp_control_plane.check_gce_range_preconditions(config, env=env, image_exists=lambda *_: False)
        assert exc.value.code == 1

    def test_image_lookup_denial_is_not_reported_as_missing(self, capsys):
        # A permission denial is not an absent image, and the opt-out flag does not hide it (#2608).
        denied = subprocess.CompletedProcess(
            [],
            1,
            "",
            "ERROR: (gcloud.compute.images.describe) Could not fetch resource:\n"
            " - Required 'compute.images.get' permission for 'projects/p/global/images/i'\n",
        )
        with patch("subprocess.run", return_value=denied), pytest.raises(SystemExit) as exc:
            gcp_control_plane.check_gce_range_preconditions(
                self._config(), allow_missing_range_images=True, env=dict(self._FULL_ENV)
            )
        assert exc.value.code == 1
        output = capsys.readouterr().out
        assert "Required 'compute.images.get' permission" in output
        assert "not imported" not in output

    def test_image_not_found_is_missing(self):
        absent = subprocess.CompletedProcess([], 1, "", " - The resource 'projects/p/global/images/i' was not found\n")
        with patch("subprocess.run", return_value=absent):
            assert gcp_control_plane._gce_image_exists("p", "image", "i") is False

    def test_allow_flag_downgrades_failures_to_warning(self, capsys):
        env = dict(self._FULL_ENV)
        del env["GCP_RANGE_DC_IMAGE"]
        # Missing var AND a missing image, but the opt-out lets a platform-first bring-up proceed.
        gcp_control_plane.check_gce_range_preconditions(
            self._config(), allow_missing_range_images=True, env=env, image_exists=lambda *_: False
        )
        output = capsys.readouterr().out
        assert "GCP_RANGE_DC_IMAGE" in output
        assert "Range guest image not imported" in output
        assert "Proceeding despite the range prerequisites" in output

    def test_preconditions_never_require_github_identities(self, capsys):
        """The range gate checks only range inputs; GitHub federation is not a deploy input (#2571)."""
        env = {k: v for k, v in self._FULL_ENV.items() if not k.startswith(("GCP_PACKER_", "GCP_WORKLOAD"))}
        gcp_control_plane.check_gce_range_preconditions(self._config(), env=env, image_exists=lambda *_: True)
        output = capsys.readouterr().out
        assert "WIF" not in output and "GCP_PACKER" not in output

    def test_image_exists_checked_in_range_cell_project_override(self):
        env = dict(self._FULL_ENV) | {"GCP_RANGE_CELL_PROJECT_ID": "range-proj"}
        seen: list[tuple[str, str, str]] = []

        def _exists(project: str, kind: str, name: str) -> bool:
            seen.append((project, kind, name))
            return True

        gcp_control_plane.check_gce_range_preconditions(self._config(), env=env, image_exists=_exists)
        # Full-URL references keep their own project; a bare/family reference would use the override.
        assert ("prod-x", "family", "shifter-ubuntu") in seen

    def test_parse_image_reference_forms(self):
        parse = gcp_control_plane._parse_gce_image_reference
        assert parse("projects/p/global/images/family/shifter-ubuntu", "d") == ("p", "family", "shifter-ubuntu")
        assert parse("projects/p/global/images/my-image-v1", "d") == ("p", "image", "my-image-v1")
        assert parse("family/shifter-kali", "d") == ("d", "family", "shifter-kali")
        assert parse("shifter-dc", "d") == ("d", "family", "shifter-dc")


def test_merge_capacity_values_preserves_unrelated_nested_chart_values():
    target = {
        "runtimeEnv": {"CLOUD_PROVIDER": "gcp"},
        "services": {"portal": {"backendConfig": {"securityPolicyName": "portal-waf"}}},
    }
    projection = {
        "runtimeEnv": {"PORTAL_WEB_WORKERS": "4"},
        "services": {"portal": {"backendConfig": {"timeoutSec": 3600}}},
    }

    merged = gcp_control_plane._merge_capacity_values(target, projection)

    assert merged is target
    assert merged["runtimeEnv"] == {"CLOUD_PROVIDER": "gcp", "PORTAL_WEB_WORKERS": "4"}
    assert merged["services"]["portal"]["backendConfig"] == {
        "securityPolicyName": "portal-waf",
        "timeoutSec": 3600,
    }
