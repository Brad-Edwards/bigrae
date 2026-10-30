#!/usr/bin/env python3
"""Publish the release authorization model to the in-cluster OpenFGA admin server (#2679).

The deploy runs this file in a short-lived Job with the ``openfga-publisher``
network lane. It uses only the standard library, the mounted admin API token,
and the mounted OpenFGA CA. It is idempotent: it reuses the one store with the
installation's name and reuses the newest model when it equals the release
model. It prints one ``BIGRAE_OPENFGA_BINDING=`` line with the store and model
IDs and never prints the token.
"""

from __future__ import annotations

import json
import os
import ssl
import sys
from collections.abc import Callable
from typing import Any
from urllib import parse, request

RESULT_PREFIX = "BIGRAE_OPENFGA_BINDING="
_MODEL_FIELDS = ("schema_version", "type_definitions", "conditions")
_TIMEOUT_SECONDS = 30


class PublishError(RuntimeError):
    """The model could not be published safely."""


def canonical(value: Any) -> Any:
    """Drop empty values, so a server readback compares equal to the release model."""
    if isinstance(value, dict):
        items = {key: canonical(item) for key, item in value.items()}
        return {key: item for key, item in sorted(items.items()) if item not in (None, "", [], {})}
    if isinstance(value, list):
        return [canonical(item) for item in value]
    return value


def model_body(model: dict[str, Any]) -> dict[str, Any]:
    """Select the fields that define an authorization model."""
    return {field: model.get(field) for field in _MODEL_FIELDS}


def same_model(published: dict[str, Any], release: dict[str, Any]) -> bool:
    """True when a published model has the release model's definition."""
    return canonical(model_body(published)) == canonical(model_body(release))


def select_store(stores: list[dict[str, Any]], name: str) -> str | None:
    """Return the ID of the one live store with ``name``, or None when there is none."""
    matches = [store for store in stores if store.get("name") == name and not store.get("deleted_at")]
    if len(matches) > 1:
        raise PublishError(f"more than one OpenFGA store is named {name!r}")
    return str(matches[0]["id"]) if matches else None


Call = Callable[[str, str, dict[str, Any] | None], dict[str, Any]]


def publish(call: Call, *, store_name: str, model: dict[str, Any]) -> dict[str, str]:
    """Reuse or create the store, then reuse or write the release model."""
    stores: list[dict[str, Any]] = []
    continuation = ""
    while True:
        query = parse.urlencode({"page_size": 100, **({"continuation_token": continuation} if continuation else {})})
        page = call("GET", f"/stores?{query}", None)
        stores.extend(page.get("stores") or [])
        continuation = str(page.get("continuation_token") or "")
        if not continuation:
            break
    store_id = select_store(stores, store_name) or str(call("POST", "/stores", {"name": store_name})["id"])
    latest = call("GET", f"/stores/{store_id}/authorization-models?page_size=1", None).get("authorization_models")
    if latest and same_model(latest[0], model):
        model_id = str(latest[0]["id"])
    else:
        written = call("POST", f"/stores/{store_id}/authorization-models", model_body(model))
        model_id = str(written["authorization_model_id"])
    return {"store_id": store_id, "model_id": model_id}


def https_call(api_url: str, token: str, ca_file: str) -> Call:
    """Return a JSON caller bound to the admin API URL, token, and CA."""
    base = parse.urlparse(api_url)
    if base.scheme != "https" or not base.netloc:
        raise PublishError("the OpenFGA admin URL must be https")
    context = ssl.create_default_context(cafile=ca_file)

    def call(method: str, path: str, body: dict[str, Any] | None) -> dict[str, Any]:
        url = f"https://{base.netloc}{path}"
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        req = request.Request(url, data=data, headers=headers, method=method)  # noqa: S310 - https only, checked above
        # The URL scheme is https and the host is the fixed admin service, both checked above.
        with request.urlopen(req, timeout=_TIMEOUT_SECONDS, context=context) as response:  # nosec B310  # noqa: S310
            return json.loads(response.read() or b"{}")

    return call


def main() -> int:
    """Publish from the mounted files and print the binding."""
    with open(os.environ["OPENFGA_TOKEN_FILE"], encoding="utf-8") as handle:
        token = handle.read().strip()
    with open(os.environ["OPENFGA_MODEL_FILE"], encoding="utf-8") as handle:
        model = json.load(handle)
    call = https_call(os.environ["OPENFGA_API_URL"], token, os.environ["OPENFGA_CA_FILE"])
    binding = publish(call, store_name=os.environ["OPENFGA_STORE_NAME"], model=model)
    print(RESULT_PREFIX + json.dumps(binding, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
