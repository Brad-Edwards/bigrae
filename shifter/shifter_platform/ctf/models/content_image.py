"""CTFContentImage — first-party copies of external images in organizer content (#2090).

The portal enforces ``img-src 'self' data:`` (ADR-036). An external image URL in
an event logo or organizer Markdown is copied into platform storage when the
content is saved, and the SPA shows the copy from the portal origin. One row
tracks one source URL for one event. Re-exported by ``ctf/models/__init__.py``.
"""

from __future__ import annotations

from django.db import models

from ._base import CTFBaseModel
from .event import CTFEvent


class ContentImageStatus(models.TextChoices):
    """Copy state of one external image."""

    PENDING = "pending", "Pending"
    READY = "ready", "Ready"
    FAILED = "failed", "Failed"


class CTFContentImage(CTFBaseModel):
    """First-party copy of one external image URL used by an event's content."""

    event = models.ForeignKey(
        CTFEvent,
        on_delete=models.CASCADE,
        related_name="content_images",
        help_text="Event whose content references the image",
    )
    source_url = models.URLField(
        max_length=2048,
        help_text="External image URL exactly as the content references it",
    )
    source_digest = models.CharField(
        max_length=64,
        help_text="SHA-256 of source_url; unique per event",
    )
    status = models.CharField(
        max_length=16,
        choices=ContentImageStatus.choices,
        default=ContentImageStatus.PENDING,
        db_index=True,
    )
    failure_code = models.CharField(
        max_length=32,
        blank=True,
        default="",
        help_text="Closed failure code when the copy failed or will retry",
    )
    attempts = models.PositiveSmallIntegerField(default=0)
    next_attempt_at = models.DateTimeField(null=True, blank=True)
    storage_key = models.CharField(max_length=512, blank=True, default="")
    content_type = models.CharField(max_length=32, blank=True, default="")
    size_bytes = models.PositiveIntegerField(default=0)
    sha256 = models.CharField(max_length=64, blank=True, default="")

    class Meta:
        """Django model metadata."""

        db_table = "ctf_content_image"
        verbose_name = "CTF Content Image"
        verbose_name_plural = "CTF Content Images"
        constraints = [
            models.UniqueConstraint(
                fields=["event", "source_digest"],
                name="unique_ctf_content_image_source",
            ),
        ]
        indexes = [
            models.Index(fields=["status", "next_attempt_at"], name="ctf_content_image_due"),
        ]

    def __str__(self) -> str:
        """Return the state and event of the image."""
        return f"{self.status} image for {self.event_id}"
