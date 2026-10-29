"""Durable original intent for one event range generation."""

from django.db import models

from shared.db import RecordedAuthorityFields

from ._base import CTFBaseModel


class CTFRangeLaunch(RecordedAuthorityFields, CTFBaseModel):
    """The primary key is also the reserved CMS and Engine request UUID."""

    event = models.ForeignKey("CTFEvent", on_delete=models.CASCADE, related_name="range_launches")
    subject_kind = models.CharField(max_length=16, editable=False)
    subject_uuid = models.UUIDField(editable=False)
    parameters = models.JSONField(editable=False)
    range_instance_id = models.PositiveIntegerField(null=True, editable=False)
    dispatched_at = models.DateTimeField(null=True, editable=False)

    class Meta:
        """Bound the only supported launch subjects."""

        constraints = [
            models.CheckConstraint(
                condition=models.Q(subject_kind__in=["participant", "spare", "rebuild"]),
                name="ctf_range_launch_subject_kind",
            )
        ]
