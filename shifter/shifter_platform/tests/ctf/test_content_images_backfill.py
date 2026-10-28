"""The #2090 backfill creates pending image rows for existing content."""

from __future__ import annotations

import importlib

import pytest
from django.apps import apps

from ctf.enums import ChallengeCategory, ChallengeDifficulty
from ctf.models import ContentImageStatus, CTFChallenge, CTFContentImage

backfill = importlib.import_module("ctf.migrations.0067_backfill_content_images").backfill


@pytest.mark.django_db
def test_backfill_records_existing_external_images_once(ctf_event):
    ctf_event.logo_url = "https://img.example.test/logo.png"
    ctf_event.rules = "![r](https://img.example.test/rules.png)"
    ctf_event.save()
    live = CTFChallenge.objects.create(
        event=ctf_event,
        name="Live",
        description="![d](https://img.example.test/live.png)",
        category=ChallengeCategory.WEB.value,
        points=100,
        difficulty=ChallengeDifficulty.EASY.value,
    )
    removed = CTFChallenge.objects.create(
        event=ctf_event,
        name="Removed",
        description="![d](https://img.example.test/removed.png)",
        category=ChallengeCategory.WEB.value,
        points=100,
        difficulty=ChallengeDifficulty.EASY.value,
    )
    removed.delete()
    CTFContentImage.objects.all().delete()

    backfill(apps, None)
    backfill(apps, None)

    rows = CTFContentImage.objects.filter(event=ctf_event)
    assert set(rows.values_list("source_url", flat=True)) == {
        "https://img.example.test/logo.png",
        "https://img.example.test/rules.png",
        "https://img.example.test/live.png",
    }
    assert set(rows.values_list("status", flat=True)) == {ContentImageStatus.PENDING}
    assert live.pk
