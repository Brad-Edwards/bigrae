"""Tests for the runtime configuration inventory checker."""

from __future__ import annotations

from pathlib import Path

import yaml

from installation.runtime_inventory import (
    GCP_CAPACITY_RUNTIME_ENV_KEYS,
    GCP_CHART_VALUES_PATHS,
    GCP_GENERATED_RUNTIME_ENV_KEYS,
    RUNTIME_SURFACES,
    RuntimeInventoryIssue,
    env_keys_from_file,
    validate_runtime_inventory,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
GCP_DEV_VALUES = "platform/charts/shifter/values-gcp-dev.yaml"


def _write_chart_values(root: Path, runtime_env: dict[str, str] | None = None) -> None:
    """Write every inventoried chart values file; ``runtime_env`` goes into the gcp-dev profile."""
    for relative_path in GCP_CHART_VALUES_PATHS:
        path = root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        values: dict[str, object] = {"runtimeEnv": {}}
        if str(relative_path) == GCP_DEV_VALUES and runtime_env is not None:
            values["runtimeEnv"] = runtime_env
        path.write_text(yaml.safe_dump(values), encoding="utf-8")


def test_repo_runtime_inventory_is_current():
    assert validate_runtime_inventory(REPO_ROOT) == []


def test_runtime_surfaces_document_root_and_mcp_policy_boundaries():
    surfaces = {surface.path: surface for surface in RUNTIME_SURFACES}

    assert surfaces["shifter.yaml"].authority == "operator-authored root installation config"
    assert surfaces[".shifter.yaml"].authority == "checked-in MCP ops policy namespace"
    assert "secret" in surfaces[".env"].notes.lower()


def test_gcp_surfaces_are_the_chart_values_of_every_supported_profile():
    """The chart is the only GCP renderer (#2571), so its committed values are the GCP surfaces."""
    surfaces = {surface.path for surface in RUNTIME_SURFACES}

    assert {str(path) for path in GCP_CHART_VALUES_PATHS} <= surfaces
    assert {str(path) for path in GCP_CHART_VALUES_PATHS} == {
        "platform/charts/shifter/values.yaml",
        "platform/charts/shifter/values-gcp-dev.yaml",
        "platform/charts/shifter/values-gcp-prod.yaml",
    }
    assert not any(path.startswith("platform/k8s/") for path in surfaces)


def test_env_keys_from_file_returns_names_without_values(tmp_path):
    env_file = tmp_path / "runtime.env"
    env_file.write_text(
        """
        # comment
        FOO=do-not-print
        BAR=another-value
        """,
        encoding="utf-8",
    )

    assert env_keys_from_file(env_file) == ("FOO", "BAR")


def test_runtime_inventory_rejects_committed_renderer_owned_keys(tmp_path):
    _write_chart_values(tmp_path, {"STORAGE_BUCKET_NAME": "placeholder", "CLOUD_PROVIDER": "gcp"})

    issues = validate_runtime_inventory(tmp_path)

    assert issues == [
        RuntimeInventoryIssue(
            GCP_DEV_VALUES,
            "renderer-owned env keys must not be committed in chart values: CLOUD_PROVIDER, STORAGE_BUCKET_NAME",
        )
    ]
    assert "placeholder" not in issues[0].render()


def test_runtime_inventory_detects_optional_secret_ref_without_echoing_the_value(tmp_path):
    _write_chart_values(tmp_path, {"GDC_VM_IMAGE_GCS_SECRET_ID": "sentinel-secret-value"})

    rendered = "\n".join(issue.render() for issue in validate_runtime_inventory(tmp_path))

    assert "GDC_VM_IMAGE_GCS_SECRET_ID" in rendered
    assert "sentinel-secret-value" not in rendered


def test_runtime_inventory_allows_static_chart_runtime_keys(tmp_path):
    _write_chart_values(tmp_path, {"RAES_OPERATION_RECORD_RETENTION_DAYS": "30"})

    assert validate_runtime_inventory(tmp_path) == []


def test_runtime_inventory_reports_missing_and_malformed_values_files(tmp_path):
    _write_chart_values(tmp_path)
    (tmp_path / "platform/charts/shifter/values-gcp-prod.yaml").unlink()
    (tmp_path / "platform/charts/shifter/values.yaml").write_text("runtimeEnv: [not, a, mapping]\n", encoding="utf-8")
    (tmp_path / GCP_DEV_VALUES).write_text("runtimeEnv: {unclosed\n", encoding="utf-8")

    missing = "required runtime inventory file is missing"
    assert validate_runtime_inventory(tmp_path) == [
        RuntimeInventoryIssue("platform/charts/shifter/values.yaml", "runtimeEnv must be a mapping"),
        RuntimeInventoryIssue(GCP_DEV_VALUES, "chart values file is not valid YAML"),
        RuntimeInventoryIssue("platform/charts/shifter/values-gcp-prod.yaml", missing),
    ]


def test_runtime_inventory_reports_a_values_file_that_is_not_a_mapping(tmp_path):
    _write_chart_values(tmp_path)
    (tmp_path / GCP_DEV_VALUES).write_text("- not\n- a mapping\n", encoding="utf-8")

    assert validate_runtime_inventory(tmp_path) == [
        RuntimeInventoryIssue(GCP_DEV_VALUES, "chart values file must be a mapping"),
    ]


def test_cloud_provider_is_a_generated_runtime_env_key():
    """CLOUD_PROVIDER is renderer-owned (PLAT-2005): the GCP backend runtime-env

    renderer emits it, so committed chart values must not also declare it.
    """
    assert "CLOUD_PROVIDER" in GCP_GENERATED_RUNTIME_ENV_KEYS


def test_capacity_runtime_keys_have_a_distinct_projection_inventory():
    assert {
        "GUACAMOLE_BOOTSTRAP_WORKERS",
        "PORTAL_WEB_GRACEFUL_TIMEOUT",
        "PORTAL_WEB_WORKERS",
        "PORTAL_WEB_WS_PING_INTERVAL",
        "PORTAL_WEB_WS_PING_TIMEOUT",
        "SHARED_SERVICE_CAPACITY_PROFILE",
    } == GCP_CAPACITY_RUNTIME_ENV_KEYS
    assert GCP_CAPACITY_RUNTIME_ENV_KEYS.isdisjoint(GCP_GENERATED_RUNTIME_ENV_KEYS)


def test_runtime_inventory_cli_check_exits_zero(capsys):
    from installation.cli import main

    assert main(["runtime-inventory", "--repo-root", str(REPO_ROOT), "--check"]) == 0

    captured = capsys.readouterr()
    assert "runtime inventory is current" in captured.out


def test_runtime_inventory_cli_lists_surfaces(capsys):
    from installation.cli import main

    assert main(["runtime-inventory"]) == 0

    captured = capsys.readouterr()
    listed_lines = [line for line in captured.out.splitlines() if line.startswith("- ")]
    assert len(listed_lines) == len(RUNTIME_SURFACES)
    assert "shifter.yaml" in captured.out
    assert ".shifter.yaml" in captured.out
    assert "values-gcp-dev.yaml" in captured.out
