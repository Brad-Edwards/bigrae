"""Publish the built image's catalog model through the real admin API.

Only file paths carry synthetic credentials. The localhost endpoint is the
disposable released server; the application gets a different token/DB duty.
"""

import argparse
import json
import ssl
from http.client import HTTPSConnection
from pathlib import Path
from urllib.parse import urlsplit


def publish(url: str, ca_file: str, token_file: str, model_file: str) -> tuple[str, str]:
    """Create a store and model, returning their non-secret identifiers."""
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "localhost"
        or parsed.port is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("fixture administration requires localhost HTTPS")
    context = ssl.create_default_context(cafile=ca_file)
    token = Path(token_file).read_text(encoding="utf-8").strip()

    def post(path: str, body: bytes) -> dict:
        connection = HTTPSConnection("localhost", parsed.port, context=context, timeout=15)
        connection.request(
            "POST",
            path,
            body=body,
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
        )
        try:
            response = connection.getresponse()
            if response.status != 201:
                raise RuntimeError(f"OpenFGA fixture publication returned HTTP {response.status}")
            return json.load(response)
        finally:
            connection.close()

    store_id = post("/stores", b'{"name":"synthetic-stack-smoke"}')["id"]
    model_id = post(f"/stores/{store_id}/authorization-models", Path(model_file).read_bytes())["authorization_model_id"]
    return store_id, model_id


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--ca-file", required=True)
    parser.add_argument("--token-file", required=True)
    parser.add_argument("--model-file", required=True)
    args = parser.parse_args()
    print(*publish(args.url, args.ca_file, args.token_file, args.model_file))
