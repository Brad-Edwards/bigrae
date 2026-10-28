"""The real maintenance runner refuses incomplete inventories and live old pods."""

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

MODULE_PATH = Path(__file__).resolve().parents[1] / "enter_identity_maintenance.py"
SPEC = importlib.util.spec_from_file_location("enter_identity_maintenance", MODULE_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def _row(name, *, phase=None, owners=()):
    row = {"metadata": {"name": name, "uid": name + "-uid", "ownerReferences": list(owners)}}
    if phase:
        row["status"] = {"phase": phase}
    return row


def _owner(kind, name):
    return {"kind": kind, "name": name, "uid": name + "-uid", "controller": True}


@pytest.fixture
def cluster(tmp_path, monkeypatch):
    """Use an executable kubectl adapter, retaining the actual subprocess/JSON path."""
    state = tmp_path / "state.json"
    log = tmp_path / "calls.jsonl"
    binary = tmp_path / "kubectl"
    binary.write_text(
        "#!/usr/bin/env python3\n"
        "import json,os,sys\n"
        "from pathlib import Path\n"
        "args=sys.argv[1:]\n"
        "with open(os.environ['CLUSTER_LOG'],'a') as out: out.write(json.dumps(args)+'\\n')\n"
        "data=json.loads(Path(os.environ['CLUSTER_STATE']).read_text())\n"
        "if args[0]=='get':\n"
        " key=args[args.index('-n')+1]+'/'+args[1]\n"
        " values=data.get('after_effects',{}) if data.get('effect_started') else {}\n"
        " value=values.get(key,data.get(key,{'items':[]}))\n"
        " if value=='error': sys.exit(7)\n"
        " if value=='invalid': print('not-json'); sys.exit(0)\n"
        " print(json.dumps(value))\n"
        "else:\n"
        " data['effect_started']=True\n"
        " Path(os.environ['CLUSTER_STATE']).write_text(json.dumps(data))\n"
        " if data.get('effect_error')==args[0]: sys.exit(8)\n"
    )
    binary.chmod(0o700)
    monkeypatch.setenv("PATH", f"{tmp_path}:{__import__('os').environ['PATH']}")
    monkeypatch.setenv("CLUSTER_STATE", str(state))
    monkeypatch.setenv("CLUSTER_LOG", str(log))

    def run(data):
        state.write_text(json.dumps(data))
        return MODULE.enter_maintenance(tmp_path / "quiesced.yaml", timeout_seconds=0)

    def calls():
        return [json.loads(line) for line in log.read_text().splitlines()]

    return run, calls


@pytest.mark.parametrize("kind", ["cronjob", "deployment", "job", "horizontalpodautoscaler", "replicaset", "pod"])
@pytest.mark.parametrize("failure", ["error", "invalid", {"items": None}, {"items": [{}]}])
def test_all_initial_inventories_must_succeed_before_any_effect(cluster, kind, failure):
    run, calls = cluster
    with pytest.raises((subprocess.CalledProcessError, ValueError)):
        run({f"shifter-jobs/{kind}": failure})
    assert all(call[0] == "get" for call in calls())


@pytest.mark.parametrize("phase", ["Pending", "Running", "Unknown"])
def test_zero_scale_and_deletion_timestamp_do_not_prove_old_pod_termination(cluster, phase):
    run, calls = cluster
    pod = _row("portal-old", phase=phase)
    pod["metadata"]["deletionTimestamp"] = "2026-01-01T00:00:00Z"
    with pytest.raises(TimeoutError):
        run({"shifter-platform/deployment": {"items": [_row("portal-web")]}, "shifter-platform/pod": {"items": [pod]}})
    assert any(call[:2] == ["scale", "deployment/portal-web"] for call in calls())
    assert not any(call[0] == "apply" for call in calls())


@pytest.mark.parametrize("phase", ["Succeeded", "Failed"])
def test_terminal_job_pods_can_remain_and_exact_policy_owner_stays_running(cluster, phase):
    run, calls = cluster
    run(
        {
            "shifter-platform/deployment": {"items": [_row("portal-web"), _row("openfga")]},
            "shifter-platform/replicaset": {"items": [_row("policy-rs", owners=[_owner("Deployment", "openfga")])]},
            "shifter-platform/pod": {
                "items": [
                    _row("finished-job", phase=phase),
                    _row("policy-pod", phase="Running", owners=[_owner("ReplicaSet", "policy-rs")]),
                ]
            },
        }
    )
    assert not any(call[:2] == ["scale", "deployment/openfga"] for call in calls())


def test_policy_name_without_exact_owner_uid_does_not_exempt_application_pod(cluster):
    run, _ = cluster
    with pytest.raises(TimeoutError):
        run({"shifter-platform/pod": {"items": [_row("openfga-imposter", phase="Running")]}})


def test_new_active_jobs_are_checked_after_suspension_and_wait_failure_aborts(cluster):
    run, calls = cluster
    with pytest.raises(subprocess.CalledProcessError):
        run(
            {
                "shifter-jobs/cronjob": {"items": [_row("cleanup")]},
                "after_effects": {
                    "shifter-jobs/job": {"items": [{**_row("cleanup-active"), "status": {"active": 1}}]},
                },
                "effect_error": "wait",
            }
        )
    history = calls()
    patch = next(i for i, call in enumerate(history) if call[0] == "patch")
    assert any(call[:2] == ["get", "job"] for call in history[patch + 1 :])
    assert history[-1][0] == "wait"


@pytest.mark.parametrize("kind", ["job", "deployment", "replicaset", "pod"])
@pytest.mark.parametrize("failure", ["error", "invalid"])
def test_post_suspension_inventory_failures_never_prove_quiescence(cluster, kind, failure):
    run, calls = cluster
    with pytest.raises((subprocess.CalledProcessError, ValueError)):
        run(
            {
                "shifter-platform/cronjob": {"items": [_row("cleanup")]},
                "after_effects": {f"shifter-jobs/{kind}": failure},
            }
        )
    history = calls()
    assert any(call[0] == "patch" for call in history)
    assert not any(call[0] == "apply" for call in history)
    assert history[-1][:2] == ["get", kind]


@pytest.mark.parametrize("effect", ["delete", "apply", "patch", "scale", "rollout"])
def test_partial_maintenance_failure_aborts_instead_of_advancing_to_migration(cluster, effect):
    run, calls = cluster
    with pytest.raises(subprocess.CalledProcessError):
        run(
            {
                "shifter-platform/horizontalpodautoscaler": {"items": [_row("portal-web")]},
                "shifter-platform/cronjob": {"items": [_row("cleanup")]},
                "shifter-platform/deployment": {"items": [_row("portal-web")]},
                "effect_error": effect,
            }
        )
    assert calls()[-1][0] == effect


def test_quiesced_policy_apply_follows_controller_and_application_pod_termination(cluster):
    run, calls = cluster
    run(
        {
            "shifter-platform/deployment": {"items": [_row("model-broker"), _row("openfga")]},
            "shifter-platform/pod": {"items": [_row("old-broker", phase="Succeeded")]},
            "shifter-jobs/job": {"items": [{**_row("active-job"), "status": {"active": 1}}]},
        }
    )
    history = calls()
    assert history[-1][0] == "apply"
    scale = next(i for i, call in enumerate(history) if call[:2] == ["scale", "deployment/model-broker"])
    rollout = next(i for i, call in enumerate(history) if call[:3] == ["rollout", "status", "deployment/model-broker"])
    job_wait = next(i for i, call in enumerate(history) if call[0] == "wait")
    final_pod_checks = [i for i, call in enumerate(history) if call[:2] == ["get", "pod"] and i > job_wait]
    assert scale < rollout < job_wait < min(final_pod_checks) <= max(final_pod_checks) < len(history) - 1
    assert len(final_pod_checks) == 2
    assert not any(call[:2] == ["scale", "deployment/openfga"] for call in history)


def test_policy_exemption_cannot_adopt_a_replaced_same_name_deployment(cluster):
    run, calls = cluster
    replacement = _row("openfga")
    replacement["metadata"]["uid"] = "replacement-uid"
    owner = _owner("Deployment", "openfga")
    owner["uid"] = "replacement-uid"
    with pytest.raises(TimeoutError):
        run(
            {
                "shifter-platform/deployment": {"items": [_row("openfga"), _row("portal-web")]},
                "after_effects": {
                    "shifter-platform/deployment": {"items": [replacement]},
                    "shifter-platform/replicaset": {"items": [_row("new-rs", owners=[owner])]},
                    "shifter-platform/pod": {
                        "items": [_row("new-policy", phase="Running", owners=[_owner("ReplicaSet", "new-rs")])]
                    },
                },
            }
        )
    assert not any(call[0] == "apply" for call in calls())


def test_policy_owner_name_must_match_its_recorded_uid(cluster):
    run, calls = cluster
    owner = _owner("Deployment", "openfga")
    owner["name"] = "foreign-workload"
    with pytest.raises(TimeoutError):
        run(
            {
                "shifter-platform/deployment": {"items": [_row("openfga")]},
                "shifter-platform/replicaset": {"items": [_row("imposter-rs", owners=[owner])]},
                "shifter-platform/pod": {
                    "items": [_row("imposter", phase="Running", owners=[_owner("ReplicaSet", "imposter-rs")])]
                },
            }
        )
    assert not any(call[0] == "apply" for call in calls())
