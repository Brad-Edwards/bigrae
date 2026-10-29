"""Synthetic device catalog rows needed by native service and HTTP fixtures."""

import pytest


@pytest.fixture(autouse=True)
def _ngfw_catalog(db):
    from cms.models import AppType, InstanceType

    InstanceType.objects.get_or_create(
        slug="panw-ngfw", defaults={"name": "PAN-OS NGFW", "spec_slug": "instance.panw-ngfw"}
    )
    AppType.objects.get_or_create(slug="panw-ngfw", defaults={"name": "PANW NGFW", "spec_slug": "app.panw-ngfw"})
