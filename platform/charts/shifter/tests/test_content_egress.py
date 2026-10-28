"""The CTF content egress proxy is the only platform pod with public egress (#2090)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import yaml

CHART = Path(__file__).resolve().parents[1]
PRIVATE_RANGES = {"10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16", "127.0.0.0/8", "100.64.0.0/10"}


def _docs(tmp_path):
    values = {
        "provider": {"name": "gcp"},
        "network": {
            "providerApiCidrs": ["199.36.153.8/30"],
            "privateServiceCidrs": ["10.60.0.0/24"],
            # Renders the metadata-server allow, so the policy set below proves
            # the proxy is excluded from workload identity as well.
            "ingressSourceCidrs": ["35.191.0.0/16"],
        },
        "runtimeEnv": {"DJANGO_SETTINGS_MODULE": "config.settings"},
    }
    path = tmp_path / "values.json"
    path.write_text(json.dumps(values))
    result = subprocess.run(
        ["helm", "template", "egress-test", str(CHART), "-f", str(path)],
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return [doc for doc in yaml.safe_load_all(result.stdout) if doc]


def _selected(selector, labels):
    if any(labels.get(key) != value for key, value in selector.get("matchLabels", {}).items()):
        return False
    for expression in selector.get("matchExpressions", []):
        value = labels.get(expression["key"])
        if expression["operator"] == "NotIn" and value in expression["values"]:
            return False
        if expression["operator"] == "In" and value not in expression["values"]:
            return False
    return True


def _policies_for(docs, labels):
    return {
        doc["metadata"]["name"]: doc["spec"]
        for doc in docs
        if doc["kind"] == "NetworkPolicy"
        and doc["metadata"]["namespace"] == "shifter-platform"
        and _selected(doc["spec"]["podSelector"], labels)
    }


def _pod(docs, name):
    return next(d for d in docs if d["kind"] == "Deployment" and d["metadata"]["name"] == name)["spec"]["template"]


def test_proxy_has_no_identity_and_only_public_https_egress(tmp_path):
    docs = _docs(tmp_path)
    pod = _pod(docs, "content-egress")
    assert "serviceAccountName" not in pod["spec"]
    assert pod["spec"]["automountServiceAccountToken"] is False
    container = pod["spec"]["containers"][0]
    assert "envFrom" not in container
    assert container["command"] == ["python", "-m", "ctf.egress_proxy"]

    policies = _policies_for(docs, pod["metadata"]["labels"])
    assert set(policies) == {"default-deny-platform", "allow-platform-dns-egress", "content-egress-boundary"}
    boundary = policies["content-egress-boundary"]
    assert boundary["ingress"] == [
        {
            "from": [
                {
                    "podSelector": {
                        "matchExpressions": [
                            {"key": "app.kubernetes.io/component", "operator": "In", "values": ["portal", "ctf-scheduler"]}
                        ]
                    }
                }
            ],
            "ports": [{"protocol": "TCP", "port": 3128}],
        }
    ]
    public = [rule for rule in boundary["egress"] if any("ipBlock" in peer for peer in rule["to"])]
    assert len(public) == 1
    assert public[0]["ports"] == [{"protocol": "TCP", "port": 443}]
    block = public[0]["to"][0]["ipBlock"]
    assert block["cidr"] == "0.0.0.0/0"
    assert PRIVATE_RANGES <= set(block["except"])


def test_only_the_portal_and_ctf_scheduler_may_use_the_proxy(tmp_path):
    docs = _docs(tmp_path)
    clients = set()
    for doc in docs:
        if doc["kind"] != "Deployment" or doc["metadata"]["namespace"] != "shifter-platform":
            continue
        labels = doc["spec"]["template"]["metadata"]["labels"]
        if "allow-ctf-content-egress-clients" in _policies_for(docs, labels):
            clients.add(labels["app.kubernetes.io/component"])
    assert clients == {"portal", "ctf-scheduler"}


def test_runtime_env_routes_ctf_egress_through_the_proxy(tmp_path):
    docs = _docs(tmp_path)
    runtime = next(d for d in docs if d["kind"] == "ConfigMap" and d["metadata"]["name"] == "platform-runtime")
    assert runtime["data"]["CTF_EGRESS_PROXY"] == "content-egress:3128"
    service = next(d for d in docs if d["kind"] == "Service" and d["metadata"]["name"] == "content-egress")
    assert service["spec"]["ports"][0]["port"] == 3128
    assert service["spec"]["selector"] == {"app.kubernetes.io/component": "content-egress"}
