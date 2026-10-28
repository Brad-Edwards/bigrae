# Installation Config

`bigrae.yaml` is the root installation config. It selects one backend bundle
and provides deployment-level settings. For local runs it lives at the
repository root. A GCP deployment that runs without GitHub keeps it in its
deployment input bundle (see the
[GitHub-less GCP deployment guide](../handbook/gcp-github-less-deployment.md)).

The authoritative parser is `shifter/installation`.

## Validate

Run from the repository root:

```bash
uv run --project shifter/installation bigrae-config validate bigrae.yaml
```

The command exits `0` when the file is valid. It exits `1` and prints all
detected issues when validation fails.

## Doctor

`validate` checks only the shape of `bigrae.yaml`. To validate the selected
backend before applying infrastructure (required tools, secret references,
generated env, owned repository paths, and the backend bundle's credential-free
validation checks, plus opt-in read-only health probes), run `doctor`:

```bash
uv run --project shifter/installation bigrae-config doctor bigrae.yaml
```

Every check is labelled local-only, cloud-read-only, or deployment-mutating, and
`doctor` is non-mutating by default. See [Setup](setup) for the full setup and
validation flow.

## Start From An Example

Scaffold a starting `bigrae.yaml` for a backend, or copy an example directly:

```bash
# Scaffold ./bigrae.yaml from the checked example (omit --backend to list backends).
uv run --project shifter/installation bigrae-config init --backend aws
# Or copy the example directly:
cp shifter/installation/examples/aws.yaml bigrae.yaml
```

Use `--backend gcp` (or `examples/gcp.yaml`) for GCP, then run `validate` and `doctor`.

## Supported Backends

| Backend | Profiles | Required secrets |
| --- | --- | --- |
| `aws` | `prod`, `dev` | `django_secret_key`, `db_password` |
| `gcp` | `prod`, `dev` | `django_secret_key` |

`validate` checks `settings` against the selected backend. GCP settings are a
closed model: unknown keys are rejected, and the model broker, model access, range
egress, warm pool, and Mission Control lease blocks are each validated against
their own schemas. See "Optional: model access broker" in
[`setup.md`](setup.md) for the broker blocks.

The GCP deploy inputs that GitHub variables and secrets held before #2571 are
closed settings blocks too:

| Block | Holds |
| --- | --- |
| `range_cells` | Range backend, zone and zone pool, range-cell project, host identity pool size, Private Google Access, and keyed guest image profiles |
| `participant_openvpn` | The participant OpenVPN opt-in (`enabled`) |
| `ctf_content_bucket` | The optional CTF content bucket name |
| `operator_bootstrap` | The first operator (`enabled`, `admin_email`, `admin_password_secret_ref`) and the staff and superuser email lists |
| `post_deploy_smoke` | The portal account of the post-deploy smoke (`user_email`) |
| `release_trust` | The immutable repository and owner IDs that sign releases (`github_repository_id`, `github_owner_id`) |

`operator_bootstrap.admin_password_secret_ref` is a versionless Secret Manager
reference (`projects/<project>/secrets/<id>`), never the password.

## Fields

| Key | Required | Notes |
| --- | --- | --- |
| `version` | no | Defaults to `1`. Only `1` is accepted. |
| `backend` | yes | Must be `aws` or `gcp`. |
| `deployment.name` | yes | Lowercase letters, digits, and internal hyphens. Length: 1-40 characters. |
| `deployment.domain` | yes | Lowercase DNS hostname with at least two labels. IP literals, schemes, trailing dots, and bare hostnames are rejected. |
| `deployment.profile` | no | Defaults to `prod`. Must be supported by the selected backend. |
| `secrets` | no | Mapping of logical secret name to a reference. Values must be references, not secret values. |
| `settings` | no | Backend-specific mapping, validated against the selected backend's settings schema. |

Secret names must match `^[a-z][a-z0-9_]*$`.

The literal secret reference `prompt` is valid for every required secret. It
records that the value must be supplied during deployment.

## Validation Rules

Validation rejects:

- missing config files
- invalid YAML
- duplicate YAML mapping keys
- YAML merge keys (`<<`)
- non-mapping top-level YAML
- unknown top-level fields
- unknown `deployment` fields
- missing required fields
- unknown backend names
- unsupported profile/backend combinations
- malformed deployment names or domains
- malformed secret names or references
- missing required backend secrets
- secret names not used by the selected backend

Validation messages identify paths and do not echo rejected input values.

## Secret Handling

`bigrae.yaml` stores secret references only.

Do not put passwords, tokens, private keys, or service-account JSON in
`bigrae.yaml`. The one exception for certificates is public trust material:
`settings.model_broker_runtime.guest_trust_ca_pem` holds the broker CA
certificate (never its key).

Secret references are checked for recognizable raw secret material, including
PEM blocks, multi-line values, and implausibly long values. Short raw values can
look like references, so `gitleaks` is still required.
