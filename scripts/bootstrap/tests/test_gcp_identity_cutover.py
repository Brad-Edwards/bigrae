"""Chart/CLI cutover ordering through an actual kubectl executable boundary."""

import json
import os
import subprocess
import sys

import pytest
import yaml
from installation.gcp_identity_cutover import probe_prerequisites, quiesce
from installation.gcp_native_identity import project_native_helm

from bootstrap_core import get_repo_root
from gcp_identity_cutover import (
    IdentityCutoverError,
    activate_identity_cutover,
    identity_cutover_boundary,
    migrate_policy_datastore,
    retire_disabled_broker,
    validate_fact_paths,
)
from gcp_process import GcpCommandError, ProcessRunner


@pytest.fixture
def chart(tmp_path):
    root = get_repo_root()
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
    users = {role: f"synthetic-{role}@synthetic-project.iam" for role in roles}
    outputs = {
        "identity_platform_project_id": {"value": "synthetic-project"},
        "workload_service_accounts": {"value": {role: user + ".gserviceaccount.com" for role, user in users.items()}},
        "control_plane_database": {"value": {"private_ip": "10.60.0.12"}},
        "native_identity": {
            "value": {
                "enabled": True,
                "openfga_enabled": True,
                "instance_connection_name": "synthetic-project:us-central1:platform-pg",
                "database_users": users,
            }
        },
    }
    values = project_native_helm(
        outputs,
        openfga_settings={
            "enabled": True,
            "runtime_secret_ref": "projects/synthetic-project/secrets/runtime",
            "admin_secret_ref": "projects/synthetic-project/secrets/admin",
            "ca_secret_ref": "projects/synthetic-project/secrets/ca",
            "store_id": "01J00000000000000000000001",
            "model_id": "01J00000000000000000000000",
        },
    )
    values["runtimeEnv"] = {
        "OPENFGA_ENABLED": "true",
        "OPENFGA_STORE_ID": values["openfga"]["storeId"],
        "OPENFGA_MODEL_ID": values["openfga"]["modelId"],
    }
    path = tmp_path / "values.json"
    path.write_text(json.dumps(values))
    rendered = subprocess.run(
        [
            "helm",
            "template",
            "native-contract",
            str(root / "platform/charts/shifter"),
            "-f",
            str(root / "platform/charts/shifter/values-gcp-dev.yaml"),
            "-f",
            str(path),
        ],
        text=True,
        capture_output=True,
        check=True,
    ).stdout
    return [doc for doc in yaml.safe_load_all(rendered) if doc]


@pytest.fixture
def cluster(tmp_path, monkeypatch):
    state, calls = tmp_path / "cluster.json", tmp_path / "calls.jsonl"
    binary = tmp_path / "kubectl"
    binary.write_text("""#!/usr/bin/env python3
import json,os,sys,yaml
from pathlib import Path
args=sys.argv[1:]
path=Path(os.environ["CLUSTER_STATE"])
state=json.loads(path.read_text())
record={"argv":args}
if args[:2]==["apply","-f"]:
    source=sys.stdin.read() if args[2]=="-" else Path(args[2]).read_text()
    record["applied"]=[{"kind":doc["kind"],"name":doc["metadata"]["name"]} for doc in yaml.safe_load_all(source) if doc]
with open(os.environ["CLUSTER_CALLS"],"a") as out: out.write(json.dumps(record)+"\\n")
if args[0]=="get":
    rows=[]
    if args[args.index("-n")+1]=="shifter-platform":
        if args[1]=="deployment": rows=[{"metadata":{"name":"portal-web","uid":"portal-uid"}}]
        if args[1]=="pod":
            phase="Succeeded" if state.get("scaled") else "Running"
            rows=[{"metadata":{"name":"old-app","uid":"pod-uid"},"status":{"phase":phase}}]
    print(json.dumps({"items":rows}))
if args[0]=="scale":
    state["scaled"]=True
    path.write_text(json.dumps(state))
if args[0]=="wait" and any(state.get("fail","NEVER") in arg for arg in args[1:]): sys.exit(9)
if args[:2]==["delete",state.get("fail_delete")]: sys.exit(8)
if args[0]=="logs" and "identity-cutover-status-" in args[1]:
    print("SHIFTER_IDENTITY_CUTOVER_RESULT="+state.get("probe",'{"state":"activated"}'))
""")
    binary.chmod(0o700)
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")
    monkeypatch.setenv("CLUSTER_STATE", str(state))
    monkeypatch.setenv("CLUSTER_CALLS", str(calls))
    state.write_text("{}")

    def configure(**values):
        state.write_text(json.dumps(values))

    def history():
        return [json.loads(line) for line in calls.read_text().splitlines()] if calls.exists() else []

    return ProcessRunner(), configure, history


@pytest.fixture
def facts(tmp_path):
    paths = (tmp_path / "cutover.json", tmp_path / "bootstrap.json")
    for path in paths:
        path.write_text('{"synthetic":"private-facts-marker"}')
        path.chmod(0o600)
    return paths


def test_real_chart_probe_retains_native_transport_and_client_identity(chart):
    prerequisites = list(yaml.safe_load_all(probe_prerequisites(yaml.safe_dump_all(chart))))
    policies = {doc["metadata"]["name"]: doc["spec"] for doc in prerequisites if doc["kind"] == "NetworkPolicy"}
    assert "allow-platform-native-sql-egress" in policies
    ports = {p["port"] for rule in policies["allow-platform-native-sql-egress"]["egress"] for p in rule["ports"]}
    assert 3307 in ports and 5432 not in ports
    assert not any(doc["kind"] in {"Deployment", "Job", "CronJob", "HorizontalPodAutoscaler"} for doc in prerequisites)
    by_name = {doc["metadata"]["name"]: doc for doc in chart if doc["kind"] == "Deployment"}
    for name, role in (
        ("portal-web", "portal"),
        ("worker-engine", "workers"),
        ("worker-provisioner-launcher", "provisioner-launcher"),
    ):
        pod = by_name[name]["spec"]["template"]["spec"]
        assert pod["initContainers"][0]["restartPolicy"] == "Always"
        assert {"name": "DB_USER", "value": f"synthetic-{role}@synthetic-project.iam"} in pod["containers"][0]["env"]
    stopped, _ = quiesce(yaml.safe_dump_all(chart))
    assert not any(doc["kind"] in {"Job", "HorizontalPodAutoscaler"} for doc in yaml.safe_load_all(stopped))


@pytest.mark.parametrize("probe", ['{"state":"unknown"}', "not-json"])
def test_inconclusive_probe_cannot_change_workloads(cluster, chart, tmp_path, probe):
    runner, configure, history = cluster
    configure(probe=probe)
    with pytest.raises(IdentityCutoverError), identity_cutover_boundary(runner, chart, tmp_path):
        pytest.fail("inconclusive state crossed boundary")
    assert not any(call["argv"][0] in {"scale", "patch", "rollout"} for call in history())
    assert not any(doc["kind"] == "Deployment" for call in history() for doc in call.get("applied", []))


def test_activated_probe_needs_no_facts_or_maintenance(cluster, chart, tmp_path):
    runner, _, history = cluster
    with identity_cutover_boundary(runner, chart, tmp_path) as run:
        assert not run.required
        activate_identity_cutover(runner, run)
    assert not any(call["argv"][0] in {"scale", "patch", "rollout"} for call in history())
    first_apply = next(call for call in history() if call.get("applied"))
    assert {"kind": "NetworkPolicy", "name": "allow-platform-native-sql-egress"} in first_apply["applied"]


@pytest.mark.parametrize("fail", [False, True])
def test_legacy_conversion_really_drains_and_never_restores_on_failure(cluster, chart, tmp_path, facts, fail, capsys):
    runner, configure, history = cluster
    configure(probe='{"state":"planned"}', fail="--timeout=30m" if fail else "NEVER")

    def convert():
        with identity_cutover_boundary(
            runner, chart, tmp_path, cutover_facts=facts[0], bootstrap_facts=facts[1]
        ) as run:
            runner.run(["kubectl", "migration-marker"])
            migrate_policy_datastore(runner, chart)
            activate_identity_cutover(runner, run)
            runner.run(["kubectl", "restore-marker"])

    if fail:
        with pytest.raises(GcpCommandError):
            convert()
    else:
        convert()
    calls = history()
    scale = next(i for i, call in enumerate(calls) if call["argv"][0] == "scale")
    migration = next(i for i, call in enumerate(calls) if call["argv"][0] == "migration-marker")
    assert scale < migration
    assert any(call["argv"][:2] == ["get", "pod"] for call in calls[scale:migration])
    policy = next(
        i
        for i, call in enumerate(calls)
        if {"kind": "Job", "name": "openfga-datastore-migrate"} in call.get("applied", [])
    )
    activation = next(
        i
        for i, call in enumerate(calls)
        if any(
            doc["kind"] == "Job" and doc["name"].startswith("identity-cutover-") and "status" not in doc["name"]
            for doc in call.get("applied", [])
        )
    )
    assert migration < policy < activation
    assert any(call["argv"][:2] == ["delete", "secret"] for call in calls)
    assert not fail or not any(call["argv"][0] == "restore-marker" for call in calls)
    assert "private-facts-marker" not in capsys.readouterr().out


def test_legacy_without_facts_does_not_drain(cluster, chart, tmp_path):
    runner, configure, history = cluster
    configure(probe='{"state":"absent"}')
    with pytest.raises(IdentityCutoverError), identity_cutover_boundary(runner, chart, tmp_path):
        pytest.fail("missing facts crossed boundary")
    assert not any(call["argv"][0] == "scale" for call in history())


@pytest.mark.parametrize("kind", ["partial", "permissions", "invalid", "duplicate", "symlink", "empty", "oversize"])
def test_fact_files_are_validated_before_any_process(cluster, facts, kind):
    _, _, history = cluster
    first, second = facts
    if kind == "permissions":
        first.chmod(0o644)
    if kind == "invalid":
        first.write_text("not-json")
    if kind == "duplicate":
        first.write_text('{"a":1,"a":2}')
    if kind == "empty":
        first.write_text("")
    if kind == "oversize":
        first.write_text("x" * 65_537)
    if kind == "symlink":
        first.unlink()
        first.symlink_to(second)
    with pytest.raises(IdentityCutoverError):
        validate_fact_paths(first, None if kind == "partial" else second)
    assert history() == []


def test_one_cli_fact_path_is_rejected_before_tool_or_cloud_calls(cluster, facts):
    from gcp_deploy import DeployOptions, run_deploy

    runner, _, history = cluster
    with pytest.raises(IdentityCutoverError):
        run_deploy(DeployOptions(source="/unused", identity_cutover_facts=str(facts[0])), runner=runner)
    assert history() == []


def test_nonregular_fact_file_is_rejected_without_waiting_for_a_writer(tmp_path, facts):
    fifo = tmp_path / "facts-pipe"
    os.mkfifo(fifo, 0o600)
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; from gcp_identity_cutover import validate_fact_paths; "
            "validate_fact_paths(sys.argv[1], sys.argv[2])",
            str(fifo),
            str(facts[1]),
        ],
        capture_output=True,
        text=True,
        timeout=3,
        check=False,
    )
    assert result.returncode != 0
    assert "regular private 0600" in result.stderr


def test_prior_migration_job_delete_failure_cannot_reuse_a_completed_job(cluster, tmp_path):
    from gcp_control_plane import run_gcp_database_migrations

    _, configure, history = cluster
    configure(fail_delete="job/platform-migrate")
    values = tmp_path / "migration-values.json"
    values.write_text(
        json.dumps(
            {
                "images": {"platform": "example.invalid/platform@sha256:" + "1" * 64},
                "runtimeEnv": {"DB_MIGRATION_SECRET_ID": "projects/synthetic-project/secrets/migration"},
            }
        )
    )
    outputs = {
        "workload_service_accounts": {"value": {"migrator": "migrator@synthetic-project.iam.gserviceaccount.com"}}
    }
    with pytest.raises(RuntimeError, match="previous GCP migration Job"):
        run_gcp_database_migrations(outputs, values)
    assert not any(call["argv"][0] == "apply" for call in history())


@pytest.mark.parametrize("failure", ["", "deployment", "networkpolicy"])
def test_disabled_broker_foreground_delete_precedes_restored_policy(cluster, failure):
    runner, configure, history = cluster
    configure(fail_delete=failure)

    def restore():
        retire_disabled_broker(runner, enabled=False)
        runner.run(["kubectl", "restore-marker"])

    if failure:
        with pytest.raises(GcpCommandError):
            restore()
        assert not any(call["argv"][0] == "restore-marker" for call in history())
    else:
        restore()
        calls = [call["argv"] for call in history()]
        deployment = next(i for i, call in enumerate(calls) if call[:2] == ["delete", "deployment"])
        network = next(i for i, call in enumerate(calls) if call[:2] == ["delete", "networkpolicy"])
        assert deployment < network < len(calls) - 1
        assert {"--cascade=foreground", "--wait=true", "--timeout=180s"} <= set(calls[deployment])
