"""Deployment input bundle for the GitHub-less GCP deploy commands (#2571, ADR-070).

A deployment keeps its deploy inputs outside the repository and outside GitHub. The
bundle is a deployment config bucket prefix (``gs://<bucket>[/<prefix>]``) or a local
directory with these objects:

``bigrae.yaml``
    The root installation config. Secrets appear only as references.
``terraform.tfvars`` and/or ``terraform.tfvars.json``
    Platform Terraform values: sizing, network ranges, the identity allow-list. When
    both exist, the JSON file comes later, so its values win (Terraform var-file order).
``range-images.json``
    Generated exact GCE image references for the range guest roles.
``foundation-inputs.json``, ``model-broker-overlay.template.json``,
``retired-cluster-artifacts.json``
    Optional inputs for the narrow foundation reconcile, the parked broker, and the
    retirement of manual qualification objects.

The loader copies the objects into a private working directory (0700 directory, 0600
files), validates them, and never prints their contents.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from gcp_process import GcpCommandError, ProcessRunner

CONFIG_OBJECT = "bigrae.yaml"
# In Terraform precedence order: a later file wins.
TERRAFORM_VALUES_OBJECTS = ("terraform.tfvars", "terraform.tfvars.json")
RANGE_IMAGES_OBJECT = "range-images.json"
FOUNDATION_INPUTS_OBJECT = "foundation-inputs.json"
BROKER_TEMPLATE_OBJECT = "model-broker-overlay.template.json"
RETIRED_ARTIFACTS_OBJECT = "retired-cluster-artifacts.json"
LOCK_OBJECT = "locks/deploy.lock"
BUNDLE_OBJECTS = (
    CONFIG_OBJECT,
    *TERRAFORM_VALUES_OBJECTS,
    RANGE_IMAGES_OBJECT,
    FOUNDATION_INPUTS_OBJECT,
    BROKER_TEMPLATE_OBJECT,
    RETIRED_ARTIFACTS_OBJECT,
)
RANGE_IMAGE_ROLES = ("linux", "kali", "windows", "dc")
RANGE_IMAGE_ENV = {
    "linux": "GCP_RANGE_LINUX_IMAGE",
    "kali": "GCP_RANGE_KALI_IMAGE",
    "windows": "GCP_RANGE_WINDOWS_IMAGE",
    "dc": "GCP_RANGE_DC_IMAGE",
}

_GCS_URL_RE = re.compile(r"^gs://[a-z0-9][a-z0-9._-]{1,61}[a-z0-9](?:/[A-Za-z0-9._-]+)*/?$")
_ENVIRONMENT_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_RANGE_IMAGE_RE = re.compile(r"^projects/[a-z][a-z0-9-]{4,28}[a-z0-9]/global/images/(?:family/)?[a-z][a-z0-9-]{0,62}$")
_MAX_INPUT_BYTES = 1_048_576


class DeploymentInputError(ValueError):
    """The deployment input bundle is missing, malformed, or inconsistent."""


def is_gcs_url(source: str) -> bool:
    """Return True when ``source`` names a config bucket prefix."""
    return source.startswith("gs://")


def gcs_object_url(source: str, name: str) -> str:
    """Join a bucket prefix and an object name."""
    if not _GCS_URL_RE.fullmatch(source):
        raise DeploymentInputError("deployment store must be gs://<bucket>[/<prefix>]")
    return f"{source.rstrip('/')}/{name}"


def private_workdir(root: Path) -> Path:
    """Create a private 0700 working directory under ``root``."""
    path = Path(root)
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.chmod(0o700)
    return path


def write_private_file(path: Path, text: str) -> Path:
    """Write ``text`` to a new 0600 file, replacing any earlier file."""
    path.unlink(missing_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(text)
    return path


def _copy_local(source: Path, workdir: Path) -> dict[str, Path]:
    """Copy the bundle files of a local directory into ``workdir``."""
    if not source.is_dir():
        raise DeploymentInputError(f"deployment input directory {source} does not exist")
    copied: dict[str, Path] = {}
    for name in BUNDLE_OBJECTS:
        candidate = source / name
        if candidate.is_file():
            if candidate.stat().st_size > _MAX_INPUT_BYTES:
                raise DeploymentInputError(f"{name} exceeds {_MAX_INPUT_BYTES} bytes")
            target = workdir / name
            target.unlink(missing_ok=True)
            shutil.copyfile(candidate, target)
            target.chmod(0o600)
            copied[name] = target
    return copied


def _copy_gcs(source: str, workdir: Path, runner: ProcessRunner, env: Mapping[str, str | None]) -> dict[str, Path]:
    """Copy the bundle objects of a ``gs://`` config store into ``workdir``."""
    base = gcs_object_url(source, "").rstrip("/")
    listing = runner.run(["gcloud", "storage", "ls", f"{base}/"], env=env).stdout.split()
    present = {url.rsplit("/", 1)[-1] for url in listing if url.startswith(f"{base}/") and not url.endswith("/")}
    copied: dict[str, Path] = {}
    for name in BUNDLE_OBJECTS:
        if name not in present:
            continue
        target = workdir / name
        target.unlink(missing_ok=True)
        runner.run(["gcloud", "storage", "cp", "--quiet", f"{base}/{name}", str(target)], env=env)
        target.chmod(0o600)
        if target.stat().st_size > _MAX_INPUT_BYTES:
            raise DeploymentInputError(f"{name} exceeds {_MAX_INPUT_BYTES} bytes")
        copied[name] = target
    return copied


def fetch_bundle(source: str, workdir: Path, runner: ProcessRunner, env: Mapping[str, str | None]) -> dict[str, Path]:
    """Copy the bundle objects that exist at ``source`` into ``workdir``."""
    private_workdir(workdir)
    if is_gcs_url(source):
        return _copy_gcs(source, workdir, runner, env)
    return _copy_local(Path(source), workdir)


def _read_json_object(path: Path, what: str) -> dict[str, object]:
    """Read a JSON file that must hold one object."""
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raise DeploymentInputError(f"{what} must be a readable JSON object") from None
    if not isinstance(value, dict):
        raise DeploymentInputError(f"{what} must be a JSON object")
    return value


def parse_range_images(path: Path | None) -> dict[str, str]:
    """Validate ``range-images.json``: role -> exact GCE image or image-family reference."""
    if path is None:
        return {}
    raw = _read_json_object(path, RANGE_IMAGES_OBJECT)
    unknown = sorted(set(raw) - set(RANGE_IMAGE_ROLES))
    if unknown:
        raise DeploymentInputError(f"{RANGE_IMAGES_OBJECT} has unsupported roles: {', '.join(unknown)}")
    images: dict[str, str] = {}
    for role, reference in raw.items():
        if not isinstance(reference, str) or not _RANGE_IMAGE_RE.fullmatch(reference):
            raise DeploymentInputError(
                f"{RANGE_IMAGES_OBJECT} role {role!r} must be projects/<project>/global/images/[family/]<name>"
            )
        images[role] = reference
    return images


def validate_environment(environment: str) -> str:
    """Return ``environment`` when it follows the deployment naming grammar."""
    if not _ENVIRONMENT_RE.fullmatch(environment) or len(environment) > 40:
        raise DeploymentInputError("environment must use lowercase letters, digits, and single internal hyphens")
    return environment


@dataclass(frozen=True)
class DeploymentInputs:
    """Validated deployment inputs, held in a private working directory."""

    source: str
    workdir: Path
    environment: str
    config_path: Path
    root_config: object
    settings: object
    terraform_values_paths: tuple[Path, ...]
    range_images: dict[str, str] = field(default_factory=dict)
    foundation_inputs_path: Path | None = None
    broker_template_path: Path | None = None
    retired_artifacts_path: Path | None = None

    @property
    def project_id(self) -> str:
        """Deployment project from ``settings.project_id``."""
        return str(self.settings.project_id)  # type: ignore[attr-defined]

    @property
    def region(self) -> str:
        """Deployment region from ``settings.region``."""
        return str(self.settings.region)  # type: ignore[attr-defined]

    @property
    def public_hostname(self) -> str:
        """Public hostname from ``deployment.domain``."""
        return str(self.root_config.deployment.domain)  # type: ignore[attr-defined]

    @property
    def profile(self) -> str:
        """Deployment profile (``dev`` or ``prod``) that selects the chart values file."""
        return str(self.root_config.deployment.profile)  # type: ignore[attr-defined]

    def range_image_env(self) -> dict[str, str]:
        """Project ``range-images.json`` onto the runtime renderer image keys."""
        return {RANGE_IMAGE_ENV[role]: reference for role, reference in self.range_images.items()}


def load_deployment_inputs(
    source: str,
    workdir: Path,
    runner: ProcessRunner,
    env: Mapping[str, str | None],
    *,
    environment: str = "",
) -> DeploymentInputs:
    """Fetch and validate a deployment input bundle.

    ``environment`` overrides ``deployment.name`` as the deployment ID (CI passes the
    workflow environment).
    """
    from installation.errors import InstallationConfigError
    from installation.loader import load_root_config
    from installation.settings_gcp import GcpBackendSettings

    copied = fetch_bundle(source, workdir, runner, env)
    config_path = copied.get(CONFIG_OBJECT)
    if config_path is None:
        raise DeploymentInputError(f"deployment inputs must include {CONFIG_OBJECT}")
    try:
        root_config = load_root_config(config_path)
    except InstallationConfigError as exc:
        raise DeploymentInputError(str(exc)) from None
    if root_config.backend != "gcp":
        raise DeploymentInputError("the GCP deploy commands require backend: gcp")
    settings = GcpBackendSettings.from_root_settings(root_config.settings)
    selected_environment = validate_environment(environment or root_config.deployment.name)

    values_paths = tuple(copied[name] for name in TERRAFORM_VALUES_OBJECTS if name in copied)
    if not values_paths:
        raise DeploymentInputError("deployment inputs must include terraform.tfvars or terraform.tfvars.json")
    for values_path in values_paths:
        if values_path.suffix == ".json":
            _read_json_object(values_path, values_path.name)

    return DeploymentInputs(
        source=source,
        workdir=workdir,
        environment=selected_environment,
        config_path=config_path,
        root_config=root_config,
        settings=settings,
        terraform_values_paths=values_paths,
        range_images=parse_range_images(copied.get(RANGE_IMAGES_OBJECT)),
        foundation_inputs_path=copied.get(FOUNDATION_INPUTS_OBJECT),
        broker_template_path=copied.get(BROKER_TEMPLATE_OBJECT),
        retired_artifacts_path=copied.get(RETIRED_ARTIFACTS_OBJECT),
    )


def operator_password_reference(inputs: DeploymentInputs) -> str:
    """Return the operator password secret: the configured one, or the one that gcp-install creates (#2679)."""
    operator = inputs.settings.operator_bootstrap  # type: ignore[attr-defined]
    configured = operator.admin_password_secret_ref if operator is not None else None
    return configured or f"projects/{inputs.project_id}/secrets/shifter-{inputs.environment}-operator-password"


def resolve_operator_password(
    inputs: DeploymentInputs,
    runner: ProcessRunner,
    env: Mapping[str, str | None],
    *,
    password_env: str = "",
    process_env: Mapping[str, str] | None = None,
) -> str:
    """Return the initial operator password from Secret Manager or an explicit env name.

    The deploy requires ``settings.operator_bootstrap``. A disabled block returns an
    empty string. The value is held in memory only.
    """
    operator = inputs.settings.operator_bootstrap  # type: ignore[attr-defined]
    if operator is None:
        raise DeploymentInputError(
            "settings.operator_bootstrap is required; set admin_email and a password source, or enabled: false"
        )
    if not operator.enabled:
        return ""
    if password_env:
        value = (process_env if process_env is not None else os.environ).get(password_env, "")
        if not value:
            raise DeploymentInputError(f"operator password environment variable {password_env} is empty")
        return value
    reference = operator_password_reference(inputs)
    _, project, _, secret = reference.split("/", 3)
    try:
        result = runner.run(
            ["gcloud", "secrets", "versions", "access", "latest", "--secret", secret, "--project", project],
            env=env,
            sensitive=True,
        )
    except GcpCommandError:
        raise DeploymentInputError("cannot read the operator password from Secret Manager") from None
    value = result.stdout.rstrip("\n")
    if not value:
        raise DeploymentInputError("the operator password secret has an empty latest version")
    return value
