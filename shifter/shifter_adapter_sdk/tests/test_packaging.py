"""Exercise a real wheel and third-party entry point outside the source tree."""

import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path
from zipfile import ZipFile

import pytest


def _run(argv, cwd):
    environment = {key: value for key, value in os.environ.items() if key not in {"PYTHONPATH", "VIRTUAL_ENV"}}
    return subprocess.run(argv, cwd=cwd, env=environment, check=True, capture_output=True, text=True, timeout=90)


def _synthetic_wheel(root, prefix="shifter"):
    """Create an independently distributed fixture, without a source-path import."""
    wheel = root / "example_adapter-1.0-py3-none-any.whl"
    metadata = "example_adapter-1.0.dist-info"
    files = {
        "example_adapter.py": textwrap.dedent("""\
            from shifter_adapter_sdk.verification import (
                PluginDeclaration, AdapterDeclaration, AdapterOutcome, AdapterStatus, CheckReason,
            )

            def check(context):
                return AdapterOutcome(AdapterStatus.PASS, CheckReason.VERIFIED)

            def factory():
                check_declaration = AdapterDeclaration("example.ready", "Synthetic readiness", check)
                return PluginDeclaration("1", "example.adapter", "1.0", (check_declaration,))
        """),
        "example_runtime.py": textwrap.dedent("""\
            from shifter_adapter_sdk.runtime import RuntimePlan, GuestAction

            class Plugin:
                def plan(self, request):
                    return RuntimePlan(
                        protocol=request.protocol, invocation_id=request.invocation_id,
                        input_digest=request.digest, phase=request.phase, status="planned",
                        actions=[GuestAction(action_id="ready", binding="server", script="test -d /tmp")],
                    )

            def factory():
                return Plugin()
        """),
        f"{metadata}/METADATA": "Metadata-Version: 2.1\nName: example-adapter\nVersion: 1.0\n",
        f"{metadata}/WHEEL": "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        f"{metadata}/entry_points.txt": (
            f"[{prefix}.scenario_verification.adapters]\nexample = example_adapter:factory\n"
            f"[{prefix}.runtime.plugins]\nexample = example_runtime:factory\n"
        ),
    }
    files[f"{metadata}/RECORD"] = "".join(f"{name},,\n" for name in [*files, f"{metadata}/RECORD"])
    with ZipFile(wheel, "w") as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return wheel


@pytest.mark.integration
@pytest.mark.parametrize("prefix", ["shifter", "bigrae"])
def test_installed_sdk_and_external_adapter_need_no_application(tmp_path, prefix):
    uv = shutil.which("uv")
    assert uv, "Packaging conformance requires the repository's uv toolchain"
    source = Path(__file__).resolve().parents[1]
    dist = tmp_path / "dist"
    _run([uv, "build", "--wheel", "--out-dir", str(dist), str(source)], tmp_path)
    (wheel,) = dist.glob("*.whl")
    legacy_dist = tmp_path / "legacy-dist"
    _run([uv, "build", "--wheel", "--out-dir", str(legacy_dist), str(source / "legacy")], tmp_path)
    (legacy_wheel,) = legacy_dist.glob("*.whl")
    environment = tmp_path / "runtime"
    _run([sys.executable, "-m", "venv", "--without-pip", str(environment)], tmp_path)
    python = environment / "bin" / "python"
    external_wheel = _synthetic_wheel(tmp_path, prefix)
    _run(
        [
            uv,
            "pip",
            "install",
            "--python",
            str(python),
            "--no-index",
            "--no-deps",
            str(wheel),
            str(legacy_wheel),
            str(external_wheel),
        ],
        tmp_path,
    )
    script = textwrap.dedent("""\
        import importlib.util
        import sys
        from shifter_adapter_sdk.verification import discover_plugins, load_plugin, PluginSelection
        from bigrae_adapter_sdk.verification import PluginSelection as BigRAESelection
        from importlib.metadata import version

        assert PluginSelection is BigRAESelection
        assert version("bigrae-adapter-sdk") == "0.1.0"
        assert version("shifter-adapter-sdk") == "0.2.0"

        assert importlib.util.find_spec("shared") is None
        assert importlib.util.find_spec("django") is None
        candidates = discover_plugins()
        assert len(candidates) == 1
        assert "example_adapter" not in sys.modules
        plugin = load_plugin(candidates, PluginSelection("example-adapter", "1.0", "example"))
        assert plugin.plugin_id == "example.adapter"
        assert plugin.plugin_version == "1.0"
    """)
    _run([str(python), "-I", "-c", script], tmp_path)
    # The runtime extra is installed into the same application-free environment.
    # Locked setup caches artifacts, but need not cache package-index metadata.
    # Use the lock's exact artifact URLs and hashes for offline installation.
    runtime_lock = tmp_path / "pylock.toml"
    _run(
        [
            uv,
            "export",
            "--frozen",
            "--offline",
            "--no-dev",
            "--extra",
            "runtime",
            "--no-emit-project",
            "--format",
            "pylock.toml",
            "--output-file",
            str(runtime_lock),
        ],
        source,
    )
    _run([uv, "pip", "install", "--offline", "--python", str(python), "-r", str(runtime_lock)], tmp_path)
    _run([uv, "pip", "check", "--python", str(python)], tmp_path)
    runtime_script = textwrap.dedent("""\
        import importlib.util
        import subprocess
        import sys
        from uuid import uuid4
        from shifter_adapter_sdk.runtime import PROTOCOL, InspectionInput, PluginManifest, RuntimeInput, parse_result

        assert importlib.util.find_spec("shared") is None
        assert importlib.util.find_spec("django") is None
        manifest = PluginManifest(
            protocol=PROTOCOL, plugin_id="example.adapter", version="1.0", distribution="example-adapter",
            entry_point="example", worker_image="registry.example.test/plugin@sha256:" + "a" * 64,
            capabilities=["guest.verify"], required_bindings=["server"],
        )
        requests = [
            InspectionInput(protocol=PROTOCOL, phase="inspect", invocation_id=uuid4(), manifest=manifest),
            RuntimeInput(
                protocol=PROTOCOL, phase="verify", invocation_id=uuid4(), operation_id=uuid4(),
                pack_digest="sha256:" + "b" * 64, manifest=manifest, provider="gcp", range_id=1,
                targets={"server": {"node_address": "node.example", "os_family": "linux"}},
            ),
        ]
        for request, expected in zip(requests, ["compatible", "planned"]):
            result = subprocess.run(
                [sys.executable, "-I", "-m", "shifter_adapter_sdk.worker"],
                env={"SHIFTER_PLUGIN_INPUT": request.model_dump_json()},
                check=True, capture_output=True, text=True, timeout=10,
            )
            assert parse_result(result.stdout, request).status == expected
            assert not result.stderr
    """)
    if prefix == "bigrae":
        runtime_script = runtime_script.replace("from shifter_adapter_sdk.runtime", "from bigrae_adapter_sdk.runtime")
        runtime_script = runtime_script.replace("shifter_adapter_sdk.worker", "bigrae_adapter_sdk.worker")
        runtime_script = runtime_script.replace("SHIFTER_PLUGIN_INPUT", "BIGRAE_PLUGIN_INPUT")
    _run([str(python), "-I", "-c", runtime_script], tmp_path)


@pytest.mark.integration
def test_native_client_alias_installs_as_one_transport(tmp_path):
    uv = shutil.which("uv")
    assert uv
    source = Path(__file__).resolve().parents[2] / "shifter_client"
    dist = tmp_path / "dist"
    _run([uv, "build", "--wheel", "--out-dir", str(dist), str(source)], tmp_path)
    (wheel,) = dist.glob("*.whl")
    legacy_dist = tmp_path / "legacy-dist"
    _run([uv, "build", "--wheel", "--out-dir", str(legacy_dist), str(source / "legacy")], tmp_path)
    (legacy_wheel,) = legacy_dist.glob("*.whl")
    environment = tmp_path / "client-runtime"
    _run([sys.executable, "-m", "venv", "--without-pip", str(environment)], tmp_path)
    python = environment / "bin" / "python"
    _run([uv, "pip", "install", "--offline", "--python", str(python), str(wheel), str(legacy_wheel)], tmp_path)
    _run(
        [
            str(python),
            "-I",
            "-c",
            "from bigrae_client import ServiceClient\n"
            "from shifter_client import ServiceClient as Old\n"
            "assert ServiceClient is Old\n",
        ],
        tmp_path,
    )
