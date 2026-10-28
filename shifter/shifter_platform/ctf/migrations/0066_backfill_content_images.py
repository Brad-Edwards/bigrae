"""Create pending image rows for organizer content that predates #2090.

Database only: the CTF scheduler copies the images after deployment. Uses the
pure discovery function and historical models, so it never imports a service.
"""

from __future__ import annotations

import hashlib

from django.db import migrations

RESERVED_BRIEFING_SLUG = "briefing"
ANNOUNCEMENT = "announcement"


def _event_urls(apps, event) -> list[str]:
    from ctf.content_images import extract_image_urls, is_admissible_image_url

    challenge = apps.get_model("ctf", "CTFChallenge")
    page = apps.get_model("ctf", "CTFEventPage")
    notification = apps.get_model("ctf", "CTFNotification")
    texts = [event.rules]
    for description, solution in challenge.objects.filter(event_id=event.pk, deleted_at__isnull=True).values_list(
        "description", "solution"
    ):
        texts.extend((description, solution))
    texts.extend(
        page.objects.filter(event_id=event.pk, deleted_at__isnull=True)
        .exclude(slug=RESERVED_BRIEFING_SLUG)
        .values_list("body", flat=True)
    )
    texts.extend(
        notification.objects.filter(
            event_id=event.pk, deleted_at__isnull=True, notification_type=ANNOUNCEMENT
        ).values_list("body", flat=True)
    )
    found: dict[str, None] = {}
    if event.logo_url and is_admissible_image_url(event.logo_url):
        found[event.logo_url] = None
    for text in texts:
        for url in extract_image_urls(text):
            found.setdefault(url, None)
    return list(found)


def backfill(apps, schema_editor) -> None:
    event_model = apps.get_model("ctf", "CTFEvent")
    image_model = apps.get_model("ctf", "CTFContentImage")
    for event in event_model.objects.filter(deleted_at__isnull=True).only("id", "logo_url", "rules").iterator():
        image_model.objects.bulk_create(
            [
                image_model(
                    event_id=event.pk,
                    source_url=url,
                    source_digest=hashlib.sha256(url.encode("utf-8")).hexdigest(),
                )
                for url in _event_urls(apps, event)
            ],
            ignore_conflicts=True,
        )


class Migration(migrations.Migration):
    dependencies = [
        ("ctf", "0065_ctfcontentimage"),
    ]

    operations = [
        migrations.RunPython(backfill, migrations.RunPython.noop),
    ]
