"""Individual client certificate identities on the existing VPN lifecycle."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

from django.db import models

from shared.db import RecordedAuthorityFields

if TYPE_CHECKING:
    from engine.models import Request


class VpnAccessProfile(RecordedAuthorityFields):
    """Public certificate receipt bound to the admitted application credential."""

    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    range = models.ForeignKey("engine.Range", on_delete=models.PROTECT, related_name="vpn_profiles")
    generation = models.UUIDField(editable=False)
    resource_generation = models.UUIDField(editable=False)
    expires_at = models.DateTimeField()
    state = models.CharField(max_length=16, default="pending")
    certificate_serial = models.CharField(max_length=49, blank=True, default="")
    certificate_fingerprint = models.CharField(max_length=64, blank=True, default="")
    issuer_fingerprint = models.CharField(max_length=64, blank=True, default="")
    secret_ref = models.CharField(max_length=512, blank=True, default="")
    provisioner_operation_id = models.UUIDField(null=True, editable=False)
    issuance_operation_id = models.UUIDField(null=True, editable=False)
    revocation_operation_id = models.UUIDField(null=True, editable=False)
    secret_removed_at = models.DateTimeField(null=True, editable=False)
    provisioner_operation = models.CharField(max_length=64, blank=True, default="")
    revoked_at = models.DateTimeField(null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    @property
    def request(self) -> Request | None:
        return self.range.request

    class Meta:
        """Declare persistence tables, indexes and custody constraints."""

        db_table = "engine_vpn_access_profile"
        constraints = [
            models.UniqueConstraint(
                fields=["range", "generation", "resource_generation", "credential_uuid"],
                condition=models.Q(revoked_at__isnull=True),
                name="engine_vpn_profile_credential_generation",
            )
        ]


class VpnCrlState(models.Model):
    """Public, signed tenant CRL and monotonic publication revision."""

    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    requested_revision = models.PositiveBigIntegerField(default=0)
    published_revision = models.PositiveBigIntegerField(default=0)
    issuer_fingerprint = models.CharField(max_length=64, blank=True, default="")
    issuer_certificate = models.TextField(blank=True, default="")
    pem = models.TextField(blank=True, default="")
    expires_at = models.DateTimeField(null=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        """Declare persistence tables, indexes and custody constraints."""

        db_table = "engine_vpn_crl_state"

    def __str__(self) -> str:
        return f"VPN CRL {self.published_revision}"
