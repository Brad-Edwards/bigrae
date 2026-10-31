"""Minimal OCI distribution client for attestation referrers (#2571, ADR-037-R6).

The deploy command reads the Sigstore bundles that the release workflow stored as OCI
referrers of each image. This client implements only the read paths it needs from the
OCI Distribution Specification v1.1: the referrers API with the referrers tag-schema
fallback, manifest fetch, and blob fetch with digest verification. It talks only to the
registry host of the image and to the token realm that the registry names.

A release lives in a public registry (#2659). The client sends the Google access token
only to an Artifact Registry host. For any other registry it uses the anonymous bearer
token flow of the distribution specification, so a Google credential never leaves Google.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from urllib import error as urllib_error
from urllib import parse as urllib_parse
from urllib import request as urllib_request

INDEX_MEDIA_TYPE = "application/vnd.oci.image.index.v1+json"
MANIFEST_MEDIA_TYPE = "application/vnd.oci.image.manifest.v1+json"
SIGSTORE_BUNDLE_ARTIFACT_TYPE = "application/vnd.dev.sigstore.bundle.v0.3+json"
_IDENTITY_RE = re.compile(
    r"^(?P<host>[a-z0-9][a-z0-9.-]*(?::\d+)?)/(?P<name>[a-z0-9][a-z0-9._/-]*)@(?P<digest>sha256:[0-9a-f]{64})$",
    re.ASCII,
)
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_ARTIFACT_REGISTRY_SUFFIX = "-docker.pkg.dev"
_LOCATION_RE = re.compile(r"[a-z0-9-]+", re.ASCII)
_MAX_DOCUMENT_BYTES = 4 * 1024 * 1024
_TIMEOUT_SECONDS = 30


def is_artifact_registry_host(host: str) -> bool:
    """True for an Artifact Registry Docker host, the only host that gets a Google token."""
    location = host.removesuffix(_ARTIFACT_REGISTRY_SUFFIX)
    return location != host and bool(_LOCATION_RE.fullmatch(location))


def bearer_parameters(params: str) -> dict[str, str]:
    """Parse the ``key="value"`` list of a ``WWW-Authenticate`` challenge."""
    values: dict[str, str] = {}
    rest = params.strip()
    while rest:
        key, sep, rest = rest.partition('="')
        value, closed, rest = rest.partition('"')
        if not sep or not closed or not key.strip().isidentifier():
            break
        values[key.strip()] = value
        rest = rest.lstrip().removeprefix(",").lstrip()
    return values


class OciError(RuntimeError):
    """A registry read failed or returned an invalid document."""


@dataclass(frozen=True)
class ImageReference:
    """An exact image identity split into registry host, repository name, and digest."""

    host: str
    name: str
    digest: str

    @classmethod
    def parse(cls, identity: str) -> ImageReference:
        """Parse ``host/name@sha256:<hex>``."""
        match = _IDENTITY_RE.fullmatch(identity)
        if match is None:
            raise OciError("image identity must be host/repository@sha256:<64 hex>")
        return cls(match["host"], match["name"], match["digest"])


TokenProvider = Callable[[], str]


class OciClient:
    """Read referrers, manifests, and blobs from one OCI registry."""

    def __init__(self, token_provider: TokenProvider, *, opener: Callable[..., object] | None = None) -> None:
        self._token_provider = token_provider
        self._opener = opener or urllib_request.urlopen
        self._bearer: dict[str, str] = {}

    def _open(self, url: str, headers: dict[str, str]) -> tuple[int, bytes, str]:
        """GET ``url`` and return the status, the body, and the bearer challenge of a 401."""
        # Each caller validates the https scheme and the exact host of ``url`` first.
        request = urllib_request.Request(url, headers=headers, method="GET")  # noqa: S310
        try:
            with self._opener(request, timeout=_TIMEOUT_SECONDS) as response:  # type: ignore[attr-defined] # nosec B310
                body = response.read(_MAX_DOCUMENT_BYTES + 1)
                status = getattr(response, "status", 200)
        except urllib_error.HTTPError as exc:
            challenge = exc.headers.get("WWW-Authenticate", "") if exc.code == 401 and exc.headers else ""
            return exc.code, b"", challenge
        except OSError:
            # URLError and TimeoutError are OSError subclasses.
            raise OciError(f"registry {urllib_parse.urlparse(url).netloc} is unreachable") from None
        if len(body) > _MAX_DOCUMENT_BYTES:
            raise OciError("registry document exceeds the size limit")
        return status, body, ""

    def _anonymous_token(self, challenge: str) -> str:
        """Fetch an anonymous pull token from the realm of a ``Bearer`` challenge."""
        scheme, _, params = challenge.partition(" ")
        values = bearer_parameters(params)
        realm = urllib_parse.urlparse(values.get("realm", ""))
        if scheme.lower() != "bearer" or realm.scheme != "https" or not realm.netloc:
            raise OciError("registry requires an unsupported authentication scheme")
        query = urllib_parse.urlencode({key: values[key] for key in ("service", "scope") if key in values})
        url = urllib_parse.urlunparse(realm._replace(query=query, fragment=""))
        status, body, _ = self._open(url, {"Accept": "application/json"})
        if status != 200:
            raise OciError(f"registry token service returned HTTP {status}")
        document = self._json(body, "registry token response")
        token = document.get("token") or document.get("access_token")
        if not isinstance(token, str) or not token:
            raise OciError("registry token response has no token")
        return token

    def _get(self, host: str, path: str, accept: str) -> tuple[int, bytes]:
        url = f"https://{host}/v2/{path}"
        parsed = urllib_parse.urlparse(url)
        if parsed.scheme != "https" or parsed.netloc != host:
            raise OciError("refusing an unexpected registry URL")
        headers = {"Accept": accept}
        if is_artifact_registry_host(host):
            credentials = base64.b64encode(f"oauth2accesstoken:{self._token_provider()}".encode()).decode("ascii")
            headers["Authorization"] = f"Basic {credentials}"
        elif host in self._bearer:
            headers["Authorization"] = f"Bearer {self._bearer[host]}"
        status, body, challenge = self._open(url, headers)
        if status == 401 and challenge and not is_artifact_registry_host(host):
            self._bearer[host] = self._anonymous_token(challenge)
            headers["Authorization"] = f"Bearer {self._bearer[host]}"
            status, body, _ = self._open(url, headers)
        return status, body

    @staticmethod
    def _json(body: bytes, what: str) -> dict[str, object]:
        try:
            value = json.loads(body)
        except json.JSONDecodeError:
            raise OciError(f"{what} is not valid JSON") from None
        if not isinstance(value, dict):
            raise OciError(f"{what} must be a JSON object")
        return value

    def referrers(self, image: ImageReference, artifact_type: str) -> list[dict[str, object]]:
        """List referrer descriptors of ``image`` with ``artifact_type``.

        Uses the referrers API and falls back to the referrers tag schema
        (``sha256-<hex>``) when the registry does not serve the API.
        """
        query = urllib_parse.urlencode({"artifactType": artifact_type})
        status, body = self._get(image.host, f"{image.name}/referrers/{image.digest}?{query}", INDEX_MEDIA_TYPE)
        if status == 404 or not body:
            tag = image.digest.replace(":", "-")
            status, body = self._get(image.host, f"{image.name}/manifests/{tag}", INDEX_MEDIA_TYPE)
            if status == 404:
                return []
        if status != 200:
            raise OciError(f"registry returned HTTP {status} for referrers")
        index = self._json(body, "referrers index")
        manifests = index.get("manifests", [])
        if not isinstance(manifests, list):
            raise OciError("referrers index has no manifest list")
        return [
            item
            for item in manifests
            if isinstance(item, dict)
            and item.get("artifactType") == artifact_type
            and isinstance(item.get("digest"), str)
            and _DIGEST_RE.fullmatch(str(item["digest"]))
        ]

    def blob_bytes(self, image: ImageReference, digest: str, accept: str = "*/*") -> bytes:
        """Fetch a blob (or manifest when ``accept`` names one) and verify its digest."""
        if not _DIGEST_RE.fullmatch(digest):
            raise OciError("blob digest must be sha256:<64 hex>")
        kind = "manifests" if accept in (MANIFEST_MEDIA_TYPE, INDEX_MEDIA_TYPE) else "blobs"
        status, body = self._get(image.host, f"{image.name}/{kind}/{digest}", accept)
        if status != 200:
            raise OciError(f"registry returned HTTP {status} for {kind[:-1]} {digest[:19]}")
        if "sha256:" + hashlib.sha256(body).hexdigest() != digest:
            raise OciError(f"registry returned content that does not match {digest[:19]}")
        return body

    def manifest(self, image: ImageReference) -> dict[str, object]:
        """Fetch the manifest of ``image`` by its digest, verify the digest, and parse it."""
        return self._json(self.blob_bytes(image, image.digest, MANIFEST_MEDIA_TYPE), "image manifest")

    def referrer_payloads(self, image: ImageReference, artifact_type: str) -> list[bytes]:
        """Return the first-layer payload of every ``artifact_type`` referrer of ``image``."""
        payloads: list[bytes] = []
        for descriptor in self.referrers(image, artifact_type):
            manifest = self._json(
                self.blob_bytes(image, str(descriptor["digest"]), MANIFEST_MEDIA_TYPE), "referrer manifest"
            )
            layers = manifest.get("layers", [])
            if not isinstance(layers, list) or not layers or not isinstance(layers[0], dict):
                raise OciError("referrer manifest has no payload layer")
            payloads.append(self.blob_bytes(image, str(layers[0].get("digest", ""))))
        return payloads
