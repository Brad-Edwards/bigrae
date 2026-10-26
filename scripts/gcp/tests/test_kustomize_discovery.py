"""CI renders standalone roots and validates Components through their consumers."""

import os
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.parametrize("job", ["k8s-lint", "k8s-schema"])
def test_quality_discovery_does_not_render_a_component_standalone(tmp_path, job):
    workflow = yaml.safe_load((ROOT / ".github/workflows/_quality.yml").read_text())
    step = next(
        item for item in workflow["jobs"][job]["steps"] if item.get("name", "").startswith("Render kustomize roots")
    )
    discovery = step["run"].split("\n/tmp/", 1)[0]
    for name, kind in (
        ("base", "Kustomization"),
        ("components/runtime", "Component"),
        ("overlays/sample", "Kustomization"),
    ):
        directory = tmp_path / "platform/k8s/gcp" / name
        directory.mkdir(parents=True)
        (directory / "kustomization.yaml").write_text(f"kind: {kind}\n")
    shim_dir = tmp_path / "bin"
    shim_dir.mkdir()
    shim = shim_dir / "kustomize"
    shim.write_text(
        "#!/bin/sh\n"
        'printf "%s\\n" "$2" >> "$DISCOVERED_ROOTS"\n'
        'if [ "$2" = "platform/k8s/gcp/components/runtime" ]; then exit 22; fi\n'
        'printf "apiVersion: v1\\nkind: ConfigMap\\nmetadata:\\n  name: synthetic\\n"\n'
    )
    shim.chmod(0o700)
    discovered = tmp_path / "discovered-roots"
    result = subprocess.run(
        ["bash", "-c", discovery],
        cwd=tmp_path,
        env={
            "PATH": f"{shim_dir}{os.pathsep}{os.defpath}",
            "RUNNER_TEMP": str(tmp_path),
            "DISCOVERED_ROOTS": str(discovered),
        },
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert set(discovered.read_text().splitlines()) == {"platform/k8s/gcp/base", "platform/k8s/gcp/overlays/sample"}
