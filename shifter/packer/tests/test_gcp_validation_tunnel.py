"""Exercise the runner's guest-boot retry boundary without cloud resources."""

import os
import subprocess
from pathlib import Path

import pytest


def _run_gather(tmp_path, image_type, guest_result, floor_result=0):
    """Run gather-evidence.sh with fake gcloud and ssh. The fake ssh fails once
    while the guest boots, then returns the result for the script it receives."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    scripts = {
        # Simulate gcloud's early connection check failing while the VM boots.
        # Disabling that check allows the listener to serve subsequent probes.
        "gcloud": """#!/bin/bash
case " $* " in
  *" --iap-tunnel-disable-connection-check "*) touch "$TEST_STATE/ready" ;;
  *) exit 1 ;;
esac
exec /bin/sleep 30
""",
        "timeout": """#!/bin/bash
/bin/sleep 0.02
test -f "$TEST_STATE/ready"
""",
        "sleep": "#!/bin/bash\nexec /bin/sleep 0.02\n",
        "ssh": """#!/bin/bash
script="$(cat)"
if grep -q php-security-floor <<< "$script"; then check=floor; else check=linux; fi
echo "$check" >> "$TEST_STATE/attempts"
if [[ ! -f "$TEST_STATE/booted" ]]; then
  touch "$TEST_STATE/booted"
  exit 255
fi
if [[ "$check" == floor ]]; then exit "$TEST_FLOOR_RESULT"; fi
exit "$TEST_GUEST_RESULT"
""",
    }
    for name, content in scripts.items():
        path = bin_dir / name
        path.write_text(content)
        path.chmod(0o755)
    env = dict(
        os.environ,
        PATH=f"{bin_dir}:{os.environ['PATH']}",
        TEST_STATE=str(tmp_path),
        TEST_GUEST_RESULT=str(guest_result),
        TEST_FLOOR_RESULT=str(floor_result),
        VM="synthetic-candidate",
        ZONE="us-central1-a",
        GCP_PROJECT_ID="example-project",
        IMAGE_TYPE=image_type,
        SSH_KEY=str(tmp_path / "synthetic-key"),
    )
    script = Path(__file__).parents[1] / "gcp/scripts/validate/gather-evidence.sh"
    # Only the checked-in script and test-owned executable fixtures are run.
    result = subprocess.run(  # noqa: S603
        ["/bin/bash", str(script)], env=env, capture_output=True, text=True, timeout=5
    )
    return result, (tmp_path / "attempts").read_text().splitlines()


@pytest.mark.parametrize(
    ("image_type", "guest_result", "floor_result", "expected", "checks"),
    [
        # The tunnel survives the boot, and an Ubuntu candidate also runs the
        # PHP security floor (#2556).
        ("ubuntu", 0, 0, 0, ["linux", "linux", "floor"]),
        ("ubuntu", 0, 1, 1, ["linux", "linux", "floor"]),
        # A failed health check stops before the floor.
        ("ubuntu", 1, 0, 1, ["linux", "linux"]),
        # Other Linux images do not run the Ubuntu PHP floor.
        ("kali", 0, 1, 0, ["linux", "linux"]),
    ],
)
def test_guest_checks_preserve_each_validation_result(
    tmp_path, image_type, guest_result, floor_result, expected, checks
):
    result, attempts = _run_gather(tmp_path, image_type, guest_result, floor_result)
    assert result.returncode == expected, result.stderr
    assert attempts == checks
