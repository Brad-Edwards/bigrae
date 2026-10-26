"""CTFSpareRange — principal-native prewarmed event spare pool.

Each spare is held by the event owner principal until recovery transfers it to
the participant principal. See :mod:`ctf.services.range.spares`.
"""

from __future__ import annotations

from django.core.exceptions import ValidationError
from django.db import models

from ctf.enums import SpareRangeStatus

from ._base import CTFBaseModel


class CTFSpareRange(CTFBaseModel):
    """A prewarmed range held in reserve for one CTF event's recovery pool.

    Attributes:
        event: The event this spare belongs to (spares are strictly
            event-scoped -- never shared across events).
        range_instance_id: The CMS ``RangeInstance.pk``, once resolved.
        request_id: The CMS provisioning request id.
        status: Pool lifecycle status (:class:`~ctf.enums.SpareRangeStatus`).
        consumed_by: The participant this spare was assigned to, if consumed.
        consumed_at: When this spare was consumed.
    """

    event = models.ForeignKey(
        "CTFEvent",
        on_delete=models.CASCADE,
        related_name="spare_ranges",
        help_text="Event this spare range belongs to",
    )
    team = models.ForeignKey(
        "CTFTeam",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="spare_ranges",
        help_text="Optional team whose selector explicitly includes this spare",
    )
    cohort = models.ForeignKey(
        "CTFCohort",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="spare_ranges",
        help_text="Optional cohort whose selector explicitly includes this spare",
    )
    range_instance_id = models.IntegerField(
        null=True,
        blank=True,
        help_text="CMS RangeInstance.pk, once known",
    )
    request_id = models.UUIDField(
        null=True,
        blank=True,
        help_text="CMS provisioning request id",
    )
    status = models.CharField(
        max_length=20,
        choices=SpareRangeStatus.choices(),
        default=SpareRangeStatus.PROVISIONING.value,
        db_index=True,
        help_text="Pool lifecycle status",
    )
    consumed_by = models.ForeignKey(
        "CTFParticipant",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="consumed_spare_ranges",
        help_text="Participant this spare was assigned to, if consumed",
    )
    consumed_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When this spare was consumed",
    )

    class Meta:
        """Django model metadata."""

        verbose_name = "CTF Spare Range"
        verbose_name_plural = "CTF Spare Ranges"
        indexes = [
            models.Index(fields=["event", "status"]),
        ]

    def __str__(self) -> str:
        return f"SpareRange({self.event_id}, {self.status})"

    def clean(self) -> None:
        errors: dict[str, list[str]] = {}
        if self.team and self.team.event_id != self.event_id:
            errors.setdefault("team", []).append("Team must belong to the same event.")
        if self.cohort and self.cohort.event_id != self.event_id:
            errors.setdefault("cohort", []).append("Cohort must belong to the same event.")
        if errors:
            raise ValidationError(errors)
