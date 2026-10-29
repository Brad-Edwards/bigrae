"""Canonical v1 API URL configuration for the Shifter platform."""

from __future__ import annotations

from django.urls import include, path
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView

from config import account_admin_urls
from config.api_bootstrap import BootstrapView
from config.api_dashboard import DashboardSummaryView
from config.api_model_access import (
    ModelAccessBindingDrainView,
    ModelAccessBindingPublishView,
    ModelAccessBindingValidateView,
    ModelAccessEffectivePolicyPreviewView,
    ModelAccessSelectorPreviewView,
)

app_name = "api"

urlpatterns = [
    # S8 coordinated activation. Register the principal-aware administration
    # composition before retained legacy includes so each colliding route has
    # one authoritative handler.
    path("", include(account_admin_urls.api_patterns)),
    path("schema/", SpectacularAPIView.as_view(api_version="v1"), name="openapi-schema"),
    path("docs/", SpectacularSwaggerView.as_view(url_name="v1:openapi-schema"), name="api-docs"),
    path("bootstrap/", BootstrapView.as_view(), name="bootstrap"),
    path("dashboard/summary/", DashboardSummaryView.as_view(), name="dashboard-summary"),
    # Scoped model-access management (M09, #2126 / PLAT-202). Cross-domain sharing
    # composition lives at the composition root; each mutation still enters the
    # owning service and compares that owner's definition revision.
    path(
        "model-access/bindings/validate/",
        ModelAccessBindingValidateView.as_view(),
        name="model-access-binding-validate",
    ),
    path(
        "model-access/bindings/selector-preview/",
        ModelAccessSelectorPreviewView.as_view(),
        name="model-access-selector-preview",
    ),
    path(
        "model-access/bindings/policy-preview/",
        ModelAccessEffectivePolicyPreviewView.as_view(),
        name="model-access-policy-preview",
    ),
    path(
        "model-access/bindings/publish/",
        ModelAccessBindingPublishView.as_view(),
        name="model-access-binding-publish",
    ),
    path(
        "model-access/bindings/drain/",
        ModelAccessBindingDrainView.as_view(),
        name="model-access-binding-drain",
    ),
    path("cms/", include("cms.api.urls", namespace="cms")),
    path("ctf/", include("ctf.api.urls")),
    path("mission-control/", include("mission_control.api.urls")),
]
