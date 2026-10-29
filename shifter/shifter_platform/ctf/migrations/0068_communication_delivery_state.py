"""Persist the occurrence delivery state (ADR-071, #1525).

The backfill derives each released intent's state from its committed rows with
the same rules as ``ctf.services.communication.disposition``. Unreleased intents
keep the ``not_released`` default.
"""

from collections import Counter

from django.db import migrations, models

_BUCKETS = {
    "queued": "pending",
    "claimed": "pending",
    "retry_due": "pending",
    "accepted": "accepted",
    "permanent_failure": "failed",
    "expired": "failed",
    "cancelled": "withdrawn",
    "suppressed": "withdrawn",
}


def _derive(intent, snapshot_model, receipt_model, attempt_model):
    recipients = snapshot_model.objects.filter(intent_id=intent.pk).count()
    if recipients == 0:
        return "empty"
    tally = Counter()
    for channel in intent.channels:
        if channel == "in_app":
            available = receipt_model.objects.filter(snapshot__intent_id=intent.pk).count()
            tally["accepted"] += available
            tally["failed"] += recipients - available
            continue
        statuses = list(
            attempt_model.objects.filter(intent_id=intent.pk, channel=channel).values_list("status", flat=True)
        )
        for status in statuses:
            tally[_BUCKETS[status]] += 1
        tally["failed"] += max(recipients - len(statuses), 0)
    if tally["pending"]:
        return "pending"
    if tally["accepted"] == sum(tally.values()):
        return "accepted"
    if tally["accepted"]:
        return "partial"
    return "failed" if tally["failed"] else "withdrawn"


def backfill_delivery_state(apps, schema_editor):
    intent_model = apps.get_model("ctf", "CommunicationIntent")
    snapshot_model = apps.get_model("ctf", "RecipientSnapshot")
    receipt_model = apps.get_model("ctf", "ParticipantReceipt")
    attempt_model = apps.get_model("ctf", "DeliveryAttempt")
    released = intent_model.objects.filter(released_at__isnull=False).only("id", "channels", "released_at")
    for intent in released.iterator(chunk_size=500):
        state = _derive(intent, snapshot_model, receipt_model, attempt_model)
        intent_model.objects.filter(pk=intent.pk).update(
            delivery_state=state, delivery_state_changed_at=intent.released_at
        )


class Migration(migrations.Migration):
    dependencies = [
        ("ctf", "0067_backfill_content_images"),
    ]

    operations = [
        migrations.AddField(
            model_name="communicationintent",
            name="delivery_state",
            field=models.CharField(
                choices=[
                    ("not_released", "Not Released"),
                    ("pending", "Pending"),
                    ("accepted", "Accepted"),
                    ("partial", "Partial"),
                    ("failed", "Failed"),
                    ("withdrawn", "Withdrawn"),
                    ("empty", "Empty"),
                ],
                default="not_released",
                help_text="Persisted aggregate delivery outcome, written only by disposition.refresh_delivery_state",
                max_length=16,
            ),
        ),
        migrations.AddField(
            model_name="communicationintent",
            name="delivery_state_changed_at",
            field=models.DateTimeField(blank=True, help_text="When delivery_state last changed", null=True),
        ),
        migrations.AddIndex(
            model_name="communicationintent",
            index=models.Index(fields=["delivery_state"], name="ctf_communi_deliver_adcfc6_idx"),
        ),
        migrations.AddConstraint(
            model_name="communicationintent",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    (
                        "delivery_state__in",
                        ["not_released", "pending", "accepted", "partial", "failed", "withdrawn", "empty"],
                    )
                ),
                name="ctf_comm_intent_delivery_state_valid",
            ),
        ),
        migrations.RunPython(backfill_delivery_state, migrations.RunPython.noop),
    ]
