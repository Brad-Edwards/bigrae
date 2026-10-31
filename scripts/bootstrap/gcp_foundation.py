"""Bootstrap the backend and independently owned GCP identity root.

The inputs select one of two modes:

- GitHub mode: ``purpose_contexts`` names the GitHub execution tuples. The root
  creates the Workload Identity pool and provider for the verified repository.
  A developer build project (#2668) uses this mode with only the ``build`` and
  ``validate`` purposes; after the apply, the bootstrap writes the
  ``gcp-image-build`` and ``gcp-image-validate`` Environments of that repository.
- GitHub-less mode (ADR-070): ``purpose_contexts`` is absent or empty. The root
  creates no pool or provider and needs no GitHub repository or owner IDs. The
  named ``impersonation_principals`` may impersonate the deploy, release-scan,
  and destroy identities, and the root creates the deployment config bucket.

Before the apply, the bootstrap compares the saved plan with the trust that the
inputs authorize (``check_tf_gcp_wif_trust/_resolved_plan.py``), so a plan can not
add a provider, a federated binding, or an impersonation grant that the inputs
do not name.
"""

import importlib.util
import json
import re
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from bootstrap_core import confirm, gcloud_resource_exists, get_repo_root, run_cmd, run_cmd_secret_stdin
from gcp_process import scoped_environment

# A destroy workflow rejects every other dispatch ref before auth; the
# cicd-oidc purpose_contexts validation enforces the same set.
DESTROY_PROTECTED_REFS = ("refs/heads/dev", "refs/heads/main")
# Purposes whose GitHub Environments the bootstrap writes (#2668).
IMAGE_PURPOSES = ("build", "validate")
# packer-gcp.yml binds these Environments and calls the validation workflow,
# so both purposes run under its workflow ref on a protected branch.
IMAGE_ENVIRONMENTS = {"build": "gcp-image-build", "validate": "gcp-image-validate"}
IMAGE_WORKFLOW = "packer-gcp.yml"
IMAGE_PROTECTED_REFS = DESTROY_PROTECTED_REFS
# Service-account ID suffix of each purpose identity (modules/cicd-oidc-identity).
_SERVICE_ACCOUNT_SUFFIXES = {
    "build": "packer",
    "validate": "validate",
    "release_scan": "scan",
    "deploy": "deploy",
    "destroy": "destroy",
}
_BRANCH_REF_RE = re.compile(r"refs/heads/([A-Za-z0-9][A-Za-z0-9_/-]*)", re.ASCII)

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
    """Reject destroy tuples that a protected-ref destroy guard can never satisfy."""
    for context in purpose_contexts.get("destroy", []):
        ref = context.get("ref")
        if ref not in DESTROY_PROTECTED_REFS or not str(context.get("workflow_ref", "")).endswith(f"@{ref}"):
            raise ValueError(
                f"Destroy purpose tuple must bind {' or '.join(DESTROY_PROTECTED_REFS)} "
                f"(the destroy guard rejects other dispatch refs); got ref={ref!r}"
            )


def _require_image_context(purpose: str, context: object, workflows: str) -> None:
    """Require the fixed Environment, a protected branch, and the packer-gcp.yml workflow ref."""
    if not isinstance(context, dict):
        raise ValueError(f"{purpose} purpose contexts must be objects")
    if context.get("environment") != IMAGE_ENVIRONMENTS[purpose]:
        raise ValueError(f"{purpose} purpose context must name the {IMAGE_ENVIRONMENTS[purpose]!r} GitHub Environment")
    ref = context.get("ref")
    if ref not in IMAGE_PROTECTED_REFS:
        raise ValueError(f"{purpose} purpose context must bind {' or '.join(IMAGE_PROTECTED_REFS)}; got ref={ref!r}")
    if context.get("workflow_ref") != f"{workflows}@{ref}" or context.get("reusable_workflow_ref"):
        raise ValueError(f"{purpose} purpose context must name the workflow {workflows}@{ref}")


def _require_image_contexts(inputs: dict[str, object]) -> None:
    """Check the contexts and buckets that the image workflows need (#2668)."""
    contexts = inputs["purpose_contexts"]
    workflows = f"{inputs['github_org']}/{inputs['github_repo']}/.github/workflows/{IMAGE_WORKFLOW}"
    for purpose in IMAGE_PURPOSES:
        for context in contexts.get(purpose, []):  # type: ignore[union-attr]
            _require_image_context(purpose, context, workflows)
    if _builds_images(inputs):
        # The image workflows store evidence in <project>-release-evidence.
        expected = f"{inputs['project_id']}-release-evidence"
        if inputs["release_evidence_bucket_name"] != expected:
            raise ValueError(
                f"Foundation input release_evidence_bucket_name must be {expected!r}; "
                "the image workflows use that bucket"
            )


def _builds_images(inputs: dict[str, object]) -> bool:
    """True when the inputs name a build or validate context."""
    contexts = inputs.get("purpose_contexts") or {}
    return any(contexts.get(purpose) for purpose in IMAGE_PURPOSES)  # type: ignore[union-attr]


def _unique_fields(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """Build a JSON object, rejecting a key that appears more than once."""
    document: dict[str, object] = {}
    for key, value in pairs:
        if key in document:
            raise ValueError(f"Foundation inputs repeat the key {key!r}")
        document[key] = value
    return document


def _load_inputs(path: Path) -> dict[str, object]:
    """Read the foundation inputs: one JSON object with unique keys."""
    try:
        inputs = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_unique_fields)
    except json.JSONDecodeError:
        raise ValueError("Foundation inputs are not valid JSON") from None
    if not isinstance(inputs, dict):
        raise ValueError("Foundation inputs must be a JSON object")
    if "purpose_contexts" in inputs and not isinstance(inputs["purpose_contexts"], dict):
        raise ValueError("Foundation input purpose_contexts must be an object")
    return inputs


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
    inputs = _load_inputs(path)
    github_mode = bool(inputs.get("purpose_contexts"))
    if github_mode:
        _require_inputs(inputs, _COMMON_INPUTS | _GITHUB_INPUTS | {"purpose_contexts"})
        _require_protected_destroy_refs(inputs["purpose_contexts"])  # type: ignore[arg-type]
        _require_image_contexts(inputs)
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
    root = get_repo_root() / "platform/terraform/gcp/global/cicd-oidc"
    tf = ["terraform", f"-chdir={root}"]
    if not dry_run:
        _confirm_foundation(inputs, project)
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
    plan = str(staging / "foundation.tfplan")
    run_cmd([*tf, "plan", "-input=false", *_var_files(inputs, path, staging), f"-out={plan}"], dry_run=dry_run)
    if not dry_run:
        _verify_saved_plan(tf, plan, inputs)
    run_cmd([*tf, "apply", "-input=false", plan], dry_run=dry_run)
    if _builds_images(inputs):
        outputs = {} if dry_run else _terraform_outputs(tf)
        publish_image_environments(inputs, outputs, dry_run=dry_run)


def _confirm_foundation(inputs: dict[str, object], project: str) -> None:
    """Check the project number and the repository IDs, then ask the operator to proceed."""
    result = run_cmd(["gcloud", "projects", "describe", project, "--format=value(projectNumber)"], capture=True)
    if result.stdout.strip() != str(inputs["project_number"]):
        raise ValueError("Foundation project number does not match the selected project")
    if inputs.get("purpose_contexts"):
        _verify_github_repository(inputs)
        trusted = f"for {inputs['github_org']}/{inputs['github_repo']}"
    else:
        trusted = "without GitHub federation"
    if not confirm(f"Bootstrap foundation in {project} {trusted}? Evidence retention locks for 90 days."):
        raise SystemExit("Foundation bootstrap cancelled")


def _var_files(inputs: dict[str, object], path: Path, staging: Path) -> list[str]:
    """Return the -var-file arguments of the foundation plan."""
    var_files = [f"-var-file={path}"]
    if not inputs.get("purpose_contexts"):
        # The federation inputs have no Terraform default (ADR-069), so the
        # GitHub-less mode sets them to empty values explicitly.
        federation_off = staging / "github-less.tfvars.json"
        federation_off.write_text(
            json.dumps({"purpose_contexts": {}, "github_repository_id": "", "github_owner_id": ""}),
            encoding="utf-8",
        )
        var_files.append(f"-var-file={federation_off}")
    return var_files


def _service_account_name(inputs: dict[str, object], purpose: str) -> str:
    """Return the resource name of one purpose identity."""
    project = inputs["project_id"]
    account = str(inputs["name_prefix"]).replace("-", "") + "-" + _SERVICE_ACCOUNT_SUFFIXES[purpose]
    return f"projects/{project}/serviceAccounts/{account}@{project}.iam.gserviceaccount.com"


def _subject_prefix(inputs: dict[str, object]) -> str:
    """Return the GitHub OIDC subject prefix that the provider binds."""
    org, repo = inputs["github_org"], inputs["github_repo"]
    if inputs.get("github_subject_format", "immutable") == "immutable":
        return f"repo:{org}@{inputs['github_owner_id']}/{repo}@{inputs['github_repository_id']}"
    return f"repo:{org}/{repo}"


def _trust_condition(inputs: dict[str, object]) -> str:
    """Render the provider condition exactly as the identity module does.

    Terraform iterates a map in key order and a list in list order.
    """
    contexts: dict[str, list[dict[str, str]]] = inputs.get("purpose_contexts") or {}  # type: ignore[assignment]
    prefix = _subject_prefix(inputs)
    alternatives = []
    for purpose in sorted(contexts):
        for context in contexts[purpose]:
            reusable = context.get("reusable_workflow_ref", "")
            clause = (
                f"(assertion.sub == '{prefix}:environment:{context['environment']}' && "
                f"assertion.ref == '{context['ref']}' && assertion.workflow_ref == '{context['workflow_ref']}'"
                + (f" && assertion.job_workflow_ref == '{reusable}'" if reusable else "")
                + ")"
            )
            alternatives.append(clause)
    return (
        f"assertion.repository == '{inputs['github_org']}/{inputs['github_repo']}' && "
        f"assertion.repository_id == '{inputs['github_repository_id']}' && "
        f"assertion.repository_owner_id == '{inputs['github_owner_id']}' && "
        "assertion.event_name == 'workflow_dispatch' && (" + " || ".join(alternatives) + ")"
    )


def expected_trust(inputs: dict[str, object]) -> dict[str, object]:
    """Derive the exact trust that the inputs authorize, for the saved-plan comparison."""
    contexts: dict[str, list[dict[str, str]]] = inputs.get("purpose_contexts") or {}  # type: ignore[assignment]
    impersonation: dict[str, list[str]] = inputs.get("impersonation_principals") or {}  # type: ignore[assignment]
    # The builder identity exists for a build purpose or a foundation image-build network (#2679).
    builder = {"build"} if inputs.get("enable_image_build_network") else set()
    purposes = {*builder, *contexts, *impersonation}
    federated = bool(contexts)
    return {
        "attribute_condition": _trust_condition(inputs) if federated else "",
        "project_id": inputs["project_id"],
        "project_number": str(inputs["project_number"]),
        "name_prefix": inputs["name_prefix"],
        "purpose_subjects": {
            purpose: sorted({f"{_subject_prefix(inputs)}:environment:{context['environment']}" for context in items})
            for purpose, items in contexts.items()
        },
        "service_accounts": {purpose: _service_account_name(inputs, purpose) for purpose in sorted(purposes)},
        "impersonation": {purpose: list(members) for purpose, members in impersonation.items()},
    }


def _load_plan_check() -> Callable[[dict[str, Any], dict[str, Any]], list[str]]:
    """Load the product saved-plan trust check from the checkout or the deployer image."""
    path = get_repo_root() / "scripts/check_tf_gcp_wif_trust/_resolved_plan.py"
    spec = importlib.util.spec_from_file_location("bigrae_resolved_wif_policy", path)
    if spec is None or spec.loader is None:
        raise ValueError("The saved-plan trust check is unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.check_resolved_plan


def _verify_saved_plan(tf: list[str], plan: str, inputs: dict[str, object]) -> None:
    """Stop before the apply when the saved plan differs from the authorized trust."""
    result = run_cmd([*tf, "show", "-json", plan], capture=True)
    try:
        document = json.loads(result.stdout or "")
    except json.JSONDecodeError:
        raise ValueError("The saved foundation plan is not readable") from None
    errors = _load_plan_check()(document, expected_trust(inputs))
    if errors:
        raise ValueError("The saved foundation plan differs from the authorized trust: " + "; ".join(errors))


def _terraform_outputs(tf: list[str]) -> dict[str, object]:
    """Return the root output values after the apply."""
    result = run_cmd([*tf, "output", "-json"], capture=True)
    try:
        document = json.loads(result.stdout or "{}")
    except json.JSONDecodeError:
        raise ValueError("The foundation outputs are not readable") from None
    return {name: item.get("value") for name, item in document.items() if isinstance(item, dict)}


def image_environment_values(
    inputs: dict[str, object], outputs: dict[str, object]
) -> dict[str, tuple[dict[str, str], dict[str, str]]]:
    """Return the secrets and variables of each image Environment (#2668)."""
    common_secrets = {
        "GCP_WORKLOAD_IDENTITY_PROVIDER": str(outputs.get("workload_identity_provider") or ""),
        "GCP_PROJECT_ID": str(inputs["project_id"]),
    }
    network = {
        "GCP_REGION": str(inputs.get("region", "us-central1")),
        "GCP_PACKER_NETWORK": str(outputs.get("image_build_network_name") or ""),
        "GCP_PACKER_SUBNETWORK": str(outputs.get("image_build_subnetwork_name") or ""),
    }
    by_purpose = {
        "build": (
            {
                **common_secrets,
                "GCP_PACKER_BUILD_SERVICE_ACCOUNT": str(outputs.get("packer_build_service_account_email") or ""),
            },
            {
                **network,
                "GCP_PACKER_USE_INTERNAL_IP": "true",
                "GCP_GCE_BASE_IMAGE_BUCKET": str(outputs.get("image_export_bucket_name") or ""),
            },
        ),
        "validate": (
            {
                **common_secrets,
                "GCP_PACKER_VALIDATE_SERVICE_ACCOUNT": str(outputs.get("packer_validate_service_account_email") or ""),
            },
            dict(network),
        ),
    }
    contexts: dict[str, list[dict[str, str]]] = inputs["purpose_contexts"]  # type: ignore[assignment]
    values: dict[str, tuple[dict[str, str], dict[str, str]]] = {}
    for purpose in IMAGE_PURPOSES:
        for context in contexts.get(purpose, []):
            values[context["environment"]] = by_purpose[purpose]
    return values


def _environment_branches(inputs: dict[str, object], environment: str) -> list[str]:
    """Return the protected branches that the contexts of one Environment name."""
    contexts: dict[str, list[dict[str, str]]] = inputs["purpose_contexts"]  # type: ignore[assignment]
    branches = {
        _BRANCH_REF_RE.fullmatch(context["ref"]).group(1)  # type: ignore[union-attr]
        for purpose in IMAGE_PURPOSES
        for context in contexts.get(purpose, [])
        if context["environment"] == environment
    }
    return sorted(branches)


def _require_image_values(values: dict[str, tuple[dict[str, str], dict[str, str]]]) -> None:
    """Stop before any write when a root output that the image workflows need is empty."""
    missing = sorted(
        {
            name
            for secrets, variables in values.values()
            for name, value in {**secrets, **variables}.items()
            if not value
        }
    )
    if missing:
        raise ValueError(
            "The build project lacks image outputs (" + ", ".join(missing) + "); "
            "set enable_image_build_network and enable_image_export_storage"
        )


def _write_environment(api: str, branches: list[str], *, dry_run: bool) -> None:
    """Create or update one Environment that admits only ``branches``."""
    run_cmd(
        [
            "gh",
            "api",
            "--method",
            "PUT",
            api,
            "-F",
            "deployment_branch_policy[protected_branches]=false",
            "-F",
            "deployment_branch_policy[custom_branch_policies]=true",
        ],
        dry_run=dry_run,
        capture=True,
    )
    existing = run_cmd(
        ["gh", "api", f"{api}/deployment-branch-policies", "--jq", ".branch_policies[].name"],
        dry_run=dry_run,
        capture=True,
    )
    present = set((existing.stdout if existing else "").split())
    for branch in branches:
        if branch not in present:
            run_cmd(
                [
                    "gh",
                    "api",
                    "--method",
                    "POST",
                    f"{api}/deployment-branch-policies",
                    "-f",
                    f"name={branch}",
                    "-f",
                    "type=branch",
                ],
                dry_run=dry_run,
                capture=True,
            )


def _write_environment_values(
    repository: str, environment: str, secrets: dict[str, str], variables: dict[str, str], *, dry_run: bool
) -> None:
    """Set the secrets on standard input and the variables of one Environment."""
    for name, value in secrets.items():
        status = run_cmd_secret_stdin(
            ["gh", "secret", "set", name, "--env", environment, "--repo", repository],
            secret_stdin=value,
            dry_run=dry_run,
        )
        if status != 0:
            raise ValueError(f"Cannot set the {name} secret of the {environment} Environment")
    for name, value in variables.items():
        run_cmd(
            ["gh", "variable", "set", name, "--env", environment, "--repo", repository, "--body", value],
            dry_run=dry_run,
            capture=True,
        )


def publish_image_environments(inputs: dict[str, object], outputs: dict[str, object], *, dry_run: bool) -> None:
    """Write the image Environments of the developer repository with ``gh`` (#2668).

    Each Environment admits only the protected branches of its contexts. Secret
    values travel to ``gh secret set`` on standard input, never in argv.
    """
    repository = f"{inputs['github_org']}/{inputs['github_repo']}"
    values = image_environment_values(inputs, outputs)
    if not dry_run:
        _require_image_values(values)
    for environment, (secrets, variables) in values.items():
        api = f"repos/{repository}/environments/{environment}"
        _write_environment(api, _environment_branches(inputs, environment), dry_run=dry_run)
        _write_environment_values(repository, environment, secrets, variables, dry_run=dry_run)
