"""The retained command delegates to the canonical installation projection."""

import subprocess
import sys
from pathlib import Path


def test_projection_command_exposes_its_file_based_cli():
    script = Path(__file__).resolve().parents[1] / "quiesce_identity_cutover_manifest.py"
    result = subprocess.run([sys.executable, str(script), "--help"], capture_output=True, text=True, check=True)
    assert "--manifest" in result.stdout and "--output" in result.stdout
