"""Validate and atomically install standard CA-signed CRLs using OpenSSL."""

import hashlib
import os
import subprocess
import tempfile
import threading
from datetime import UTC, datetime, timedelta

MAX_CRL_BYTES = 262144


class CrlStore:
    """The local CA remains authoritative; stale or rolled-back lists are refused."""

    def __init__(self, ca_path: str, path: str):
        self.ca_path, self.path = ca_path, path
        self._revision = -1
        self._digest = ""
        self._expires = datetime.min.replace(tzinfo=UTC)
        self._lock = threading.Lock()

    def install(self, pem: str) -> None:
        if not isinstance(pem, str) or not 0 < len(pem.encode()) <= MAX_CRL_BYTES:
            raise ValueError("VPN CRL unavailable")
        try:
            result = subprocess.run(  # noqa: S603
                [
                    "/usr/bin/openssl",
                    "crl",
                    "-CAfile",
                    self.ca_path,
                    "-verify",
                    "-noout",
                    "-lastupdate",
                    "-nextupdate",
                    "-crlnumber",
                ],
                input=pem.encode(),
                capture_output=True,
                timeout=5,
                check=True,
            )
            # `openssl crl -verify` can exit zero even on signature failure.
            # Require its explicit cryptographic verdict as well as process success.
            if result.stderr.strip() != b"verify OK":
                raise ValueError("VPN CRL signature unavailable")
            fields = dict(line.split("=", 1) for line in result.stdout.decode("ascii").splitlines())
            revision = int(fields["crlNumber"], 0)
            updated = datetime.strptime(fields["lastUpdate"], "%b %d %H:%M:%S %Y GMT").replace(tzinfo=UTC)
            expires = datetime.strptime(fields["nextUpdate"], "%b %d %H:%M:%S %Y GMT").replace(tzinfo=UTC)
        except (OSError, subprocess.SubprocessError, ValueError, KeyError):
            raise ValueError("VPN CRL unavailable") from None
        now = datetime.now(UTC)
        digest = hashlib.sha256(pem.encode()).hexdigest()
        with self._lock:
            if (
                not 0 <= revision < 2**63
                or expires <= now
                or updated > now + timedelta(minutes=5)
                or revision < self._revision
                or (revision == self._revision and self._digest and digest != self._digest)
            ):
                raise ValueError("VPN CRL unavailable")
            with tempfile.NamedTemporaryFile(dir=os.path.dirname(self.path), delete=False) as handle:
                temporary = handle.name
                handle.write(pem.encode())
            try:
                os.replace(temporary, self.path)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
            self._revision, self._digest, self._expires = revision, digest, expires

    def valid(self) -> bool:
        """Missing, stale or replaced local CRLs stop admission and forwarding."""
        with self._lock:
            try:
                with open(self.path, "rb") as handle:
                    data = handle.read(MAX_CRL_BYTES + 1)
                return (
                    self._expires > datetime.now(UTC)
                    and len(data) <= MAX_CRL_BYTES
                    and hashlib.sha256(data).hexdigest() == self._digest
                )
            except OSError:
                return False
