"""Post-activation catalog Job using the exact chart's portal bindings."""

from copy import deepcopy

from installation.gcp_identity_cutover import render as render_portal_job


def render_catalog_job(
    manifest: str, *, job_name: str, secret_name: str, bootstrap_facts: bool = False
) -> dict[str, object]:
    """Keep native runtime admission and copy the private input into a private file.

    The input is an issued bearer proof, or the bootstrap administrator facts of
    a GitHub-less install (#2679). Both arrive as the Secret key ``credential``.
    """
    job = render_portal_job(manifest, job_name=job_name, status_only=True)
    pod = job["spec"]["template"]["spec"]
    portal = pod["containers"][0]
    portal["args"] = (
        ["python", "manage.py", "initialize_installation_catalog", "--bootstrap-facts"]
        if bootstrap_facts
        else ["python", "manage.py", "bootstrap_inbox_catalog", "--credential-file"]
    ) + ["/run/catalog-private/credential"]
    portal["env"] = [
        item
        for item in portal.get("env", [])
        if item["name"] not in {"SHIFTER_IDENTITY_MAINTENANCE", "SKIP_MIGRATIONS"}
    ] + [{"name": "SHIFTER_IDENTITY_MAINTENANCE", "value": "false"}, {"name": "SKIP_MIGRATIONS", "value": "1"}]
    portal.setdefault("volumeMounts", []).append(
        {"name": "catalog-private", "mountPath": "/run/catalog-private", "readOnly": True}
    )
    pod.setdefault("securityContext", {}).update({"fsGroup": 1000, "fsGroupChangePolicy": "OnRootMismatch"})
    pod["initContainers"] = [
        {
            "name": "catalog-credential-prepare",
            "image": portal["image"],
            "command": ["/bin/sh", "-ceu"],
            "args": ["install -m 600 /run/catalog-source/credential /run/catalog-private/credential"],
            "securityContext": portal.get("securityContext", {}).copy(),
            "resources": deepcopy(portal.get("resources", {})),
            "volumeMounts": [
                {"name": "catalog-source", "mountPath": "/run/catalog-source", "readOnly": True},
                {"name": "catalog-private", "mountPath": "/run/catalog-private"},
            ],
        },
        *pod.get("initContainers", []),
    ]
    if "imagePullPolicy" in portal:
        pod["initContainers"][0]["imagePullPolicy"] = portal["imagePullPolicy"]
    pod.setdefault("volumes", []).extend(
        [
            {"name": "catalog-source", "secret": {"secretName": secret_name, "defaultMode": 0o440}},
            {"name": "catalog-private", "emptyDir": {"medium": "Memory", "sizeLimit": "1Mi"}},
        ]
    )
    labels = {"app.kubernetes.io/part-of": "shifter", "app.kubernetes.io/component": "catalog-initializer"}
    job["metadata"]["labels"] = labels
    job["spec"]["template"]["metadata"]["labels"] = labels
    return job
