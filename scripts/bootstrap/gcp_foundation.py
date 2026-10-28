"""Bootstrap the backend and independently owned GCP identity root.

The inputs select one of two modes:

- GitHub mode: ``purpose_contexts`` names the GitHub execution tuples. The root
  creates the Workload Identity pool and provider for the verified repository.
- GitHub-less mode (ADR-070): ``purpose_contexts`` is absent or empty. The root
  creates no pool or provider and needs no GitHub repository or owner IDs. The
  named ``impersonation_principals`` may impersonate the deploy, release-scan,
  and destroy identities, and the root creates the deployment config bucket.
"""

import json
import re
import tempfile
from pathlib import Path

from bootstrap_core import confirm, gcloud_resource_exists, get_repo_root, run_cmd
from gcp_process import scoped_environment

# gcp-dev-destroy.yml rejects every other dispatch ref before auth; the
# cicd-oidc purpose_contexts validation enforces the same set.
DESTROY_PROTECTED_REFS = ("refs/heads/dev", "refs/heads/main")

# GCP naming rules. The bucket rule matches the cicd-oidc
# terraform_state_bucket_name validation. A value that starts with "-" can
# not match, so gcloud can not parse an input as a flag.
_PROJECT_ID_RE = re.compile(r"[a-z][a-z0-9-]{4,28}[a-z0-9]")
_BUCKET_NAME_RE = re.compile(r"[a-z0-9][a-z0-9-]{1,61}[a-z0-9]")
_REGION_RE = re.compile(r"[a-z]+-[a-z]+\d+", re.ASCII)

_COMMON_INPUTS = frozenset(
    {
        "project_id",
        "project_number",
        "environment",
        "name_prefix",
        "terraform_state_bucket_name",
        "release_evidence_bucket_name",
    }
)
_GITHUB_INPUTS = frozenset({"github_org", "github_repo", "github_repository_id", "github_owner_id"})
_GITHUB_LESS_INPUTS = frozenset({"impersonation_principals", "deployment_config_bucket_name"})
# The lifecycle identities that the GitHub-less commands impersonate.
IMPERSONATED_PURPOSES = ("deploy", "release_scan", "destroy")


def _require_protected_destroy_refs(purpose_contexts: dict[str, list[dict[str, str]]]) -> None:
    """Reject destroy tuples the destroy workflow's protected-ref guard can never satisfy."""
    for context in purpose_contexts.get("destroy", []):
        ref = context.get("ref")
        if ref not in DESTROY_PROTECTED_REFS or not str(context.get("workflow_ref", "")).endswith(f"@{ref}"):
            raise ValueError(
                f"Destroy purpose tuple must bind {' or '.join(DESTROY_PROTECTED_REFS)} "
                f"(gcp-dev-destroy.yml rejects other dispatch refs); got ref={ref!r}"
            )


def _require_gcp_name(field: str, value: object, pattern: re.Pattern[str]) -> str:
    """Return a foundation input value only when it is a valid GCP resource name."""
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise ValueError(f"Foundation input {field} is not a valid GCP name: {value!r}")
    return value


def _require_inputs(inputs: dict[str, object], required: frozenset[str]) -> None:
    """Stop when any ``required`` foundation input is missing."""
    missing = sorted(required - inputs.keys())
    if missing:
        raise ValueError("Missing foundation inputs: " + ", ".join(missing))


def _validate_github_less(inputs: dict[str, object]) -> None:
    """Check the GitHub-less inputs: named principals for every lifecycle purpose and no GitHub IDs."""
    _require_inputs(inputs, _COMMON_INPUTS | _GITHUB_LESS_INPUTS)
    github = sorted(field for field in _GITHUB_INPUTS if inputs.get(field))
    if github:
        raise ValueError(
            "A foundation without GitHub federation takes no GitHub inputs; remove "
            + ", ".join(github)
            + " or set purpose_contexts"
        )
    principals = inputs["impersonation_principals"]
    if not isinstance(principals, dict) or any(
        not isinstance(principals.get(purpose), list) or not principals.get(purpose)
        for purpose in IMPERSONATED_PURPOSES
    ):
        raise ValueError(
            "impersonation_principals must name at least one principal for each of: " + ", ".join(IMPERSONATED_PURPOSES)
        )
    _require_gcp_name("deployment_config_bucket_name", inputs["deployment_config_bucket_name"], _BUCKET_NAME_RE)
    # gcp-deploy and gcp-destroy derive these bucket names from the project.
    project = inputs["project_id"]
    for field, expected in (
        ("terraform_state_bucket_name", f"{project}-terraform-state"),
        ("release_evidence_bucket_name", f"{project}-release-evidence"),
    ):
        if inputs[field] != expected:
            raise ValueError(f"Foundation input {field} must be {expected!r}; the deploy commands use that bucket")


def _verify_github_repository(inputs: dict[str, object]) -> None:
    """Bind the GitHub mode to the immutable repository IDs that GitHub reports."""
    result = run_cmd(["gh", "api", f"repos/{inputs['github_org']}/{inputs['github_repo']}"], capture=True)
    repository = json.loads(result.stdout)
    if (str(repository["id"]), str(repository["owner"]["id"])) != (
        str(inputs["github_repository_id"]),
        str(inputs["github_owner_id"]),
    ):
        raise ValueError("Foundation repository IDs do not match GitHub")


def bootstrap_gcp_foundation(inputs_path: str, *, dry_run: bool = False) -> None:
    """Apply explicit foundation inputs before runners, image bakes, or platform."""
    path = Path(inputs_path).resolve(strict=True)
    inputs = json.loads(path.read_text())
    github_mode = bool(inputs.get("purpose_contexts"))
    if github_mode:
        _require_inputs(inputs, _COMMON_INPUTS | _GITHUB_INPUTS | {"purpose_contexts"})
        _require_protected_destroy_refs(inputs["purpose_contexts"])
    else:
        _validate_github_less(inputs)
    project = _require_gcp_name("project_id", inputs["project_id"], _PROJECT_ID_RE)
    bucket = _require_gcp_name("terraform_state_bucket_name", inputs["terraform_state_bucket_name"], _BUCKET_NAME_RE)
    region = _require_gcp_name("region", inputs.get("region", "us-central1"), _REGION_RE)
    with tempfile.TemporaryDirectory(prefix="shifter-gcp-foundation-") as staging:
        # Terraform, its gcs backend, and the Google client libraries run as the
        # caller's ADC. Pin the quota project to the foundation project, so a stale
        # ADC quota project cannot fail the run (#2584). A private TF_DATA_DIR keeps
        # the backend configuration out of the checkout (#2586).
        overlay = {"GOOGLE_CLOUD_QUOTA_PROJECT": project, "TF_DATA_DIR": str(Path(staging) / "terraform-data")}
        with scoped_environment(overlay):
            _apply_foundation(
                inputs, path, Path(staging), project=project, bucket=bucket, region=region, dry_run=dry_run
            )


def _apply_foundation(
    inputs: dict[str, object], path: Path, staging: Path, *, project: str, bucket: str, region: str, dry_run: bool
) -> None:
    """Enable the APIs, create the state bucket, and apply the cicd-oidc root from a saved plan."""
    github_mode = bool(inputs.get("purpose_contexts"))
    root = get_repo_root() / "platform/terraform/gcp/global/cicd-oidc"
    tf = ["terraform", f"-chdir={root}"]
    if not dry_run:
        result = run_cmd(["gcloud", "projects", "describe", project, "--format=value(projectNumber)"], capture=True)
        if result.stdout.strip() != str(inputs["project_number"]):
            raise ValueError("Foundation project number does not match the selected project")
        if github_mode:
            _verify_github_repository(inputs)
            trusted = f"for {inputs['github_org']}/{inputs['github_repo']}"
        else:
            trusted = "without GitHub federation"
        if not confirm(f"Bootstrap foundation in {project} {trusted}? Evidence retention locks for 90 days."):
            raise SystemExit("Foundation bootstrap cancelled")
    run_cmd(
        [
            "gcloud",
            "services",
            "enable",
            "iam.googleapis.com",
            "iamcredentials.googleapis.com",
            "sts.googleapis.com",
            "compute.googleapis.com",
            "iap.googleapis.com",
            "cloudbuild.googleapis.com",
            "artifactregistry.googleapis.com",
            "storage.googleapis.com",
            "--project",
            project,
        ],
        dry_run=dry_run,
    )
    if dry_run or not gcloud_resource_exists(["gcloud", "storage", "buckets", "describe", f"gs://{bucket}"]):
        run_cmd(
            [
                "gcloud",
                "storage",
                "buckets",
                "create",
                f"gs://{bucket}",
                "--project",
                project,
                "--location",
                region,
                "--uniform-bucket-level-access",
                "--public-access-prevention",
            ],
            dry_run=dry_run,
        )
    run_cmd(["gcloud", "storage", "buckets", "update", f"gs://{bucket}", "--versioning"], dry_run=dry_run)
    run_cmd(
        [*tf, "init", "-reconfigure", f"-backend-config=bucket={bucket}", "-backend-config=prefix=cicd-oidc"],
        dry_run=dry_run,
    )
    var_files = [f"-var-file={path}"]
    if not github_mode:
        # The federation inputs have no Terraform default (ADR-069), so the
        # GitHub-less mode sets them to empty values explicitly.
        federation_off = staging / "github-less.tfvars.json"
        federation_off.write_text(
            json.dumps({"purpose_contexts": {}, "github_repository_id": "", "github_owner_id": ""}),
            encoding="utf-8",
        )
        var_files.append(f"-var-file={federation_off}")
    plan = str(staging / "foundation.tfplan")
    run_cmd([*tf, "plan", "-input=false", *var_files, f"-out={plan}"], dry_run=dry_run)
    run_cmd([*tf, "apply", "-input=false", plan], dry_run=dry_run)
