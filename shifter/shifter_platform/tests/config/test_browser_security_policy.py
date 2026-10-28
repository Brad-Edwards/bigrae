"""Tests for the browser security policy (#1520, #2090, ADR-036).

Covers the code-owned CSP candidate shape, the validated report-only|enforce
mode seam and its enforce default, the global Referrer-Policy /
Permissions-Policy / Reporting-Endpoints values, and that real middleware
responses carry the enforced header. Only the SPA host reads the per-response
style nonce, and that response is not cacheable.
"""

from __future__ import annotations

import re

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.test import Client
from django.utils.csp import CSP

from config import _browser_security as bs

_FORBIDDEN_SOURCES = frozenset(
    {
        str(CSP.UNSAFE_INLINE),
        str(CSP.UNSAFE_EVAL),
        str(CSP.UNSAFE_HASHES),
        "*",
        "http:",
        "https:",
        "data:",
    }
)


def _all_sources(candidate: dict) -> list[str]:
    sources: list[str] = []
    for values in candidate.values():
        sources.extend(str(v) for v in values)
    return sources


def test_candidate_is_deny_by_default():
    candidate = bs.build_browser_csp()
    for directive in (
        "default-src",
        "base-uri",
        "object-src",
        "frame-src",
        "media-src",
        "worker-src",
        "frame-ancestors",
        "script-src-attr",
        "style-src-attr",
    ):
        assert candidate[directive] == [CSP.NONE], directive


def test_candidate_has_no_unsafe_or_wildcard_sources():
    candidate = bs.build_browser_csp()
    # data: is permitted only for images; no other directive may carry it.
    for directive, values in candidate.items():
        rendered = {str(v) for v in values}
        forbidden = _FORBIDDEN_SOURCES - ({"data:"} if directive == "img-src" else set())
        assert not (rendered & forbidden), (directive, rendered & forbidden)


def test_candidate_scripts_and_styles_deny_attribute_variants():
    candidate = bs.build_browser_csp()
    assert candidate["script-src-attr"] == [CSP.NONE]
    assert candidate["style-src-attr"] == [CSP.NONE]
    # Scripts never get a nonce; only script-created style elements do (#2090).
    assert CSP.NONCE not in candidate["script-src"]
    assert candidate["style-src"] == [CSP.SELF, CSP.NONCE]
    assert str(CSP.UNSAFE_INLINE) not in _all_sources(candidate)


def test_candidate_routes_reports_to_same_origin_collector():
    candidate = bs.build_browser_csp()
    assert candidate["report-uri"] == [bs.CSP_REPORT_PATH]
    assert candidate["report-to"] == ["csp"]
    assert bs.CSP_REPORT_PATH == "/security/csp-report/"


def test_candidate_allows_self_and_known_origins_only():
    candidate = bs.build_browser_csp()
    assert CSP.SELF in candidate["script-src"]
    assert CSP.SELF in candidate["connect-src"]
    # Every non-sentinel source is an exact https origin (or a bounded scheme).
    for directive in ("script-src", "style-src", "connect-src"):
        for value in candidate[directive]:
            text = str(value)
            if text in {str(CSP.SELF), str(CSP.NONE), str(CSP.NONCE)}:
                continue
            assert text.startswith("https://"), (directive, text)


def test_storage_connect_origins_raises_for_unsupported_provider(monkeypatch):
    """A future third backend must fail closed rather than silently receiving the
    AWS S3 signed-upload CSP origins (the previous ``gcp ? gcs : aws`` shape
    treated any non-gcp value as AWS)."""
    monkeypatch.setattr(bs, "CLOUD_PROVIDER", "azure")

    with pytest.raises(ImproperlyConfigured, match="azure"):
        bs._storage_connect_origins()


def test_candidate_includes_each_required_dependency_origin():
    # Guard against a merge/refactor silently dropping a required origin: the
    # shape check above would still pass, but Identity Platform login or signed
    # uploads would break under enforcement.
    candidate = bs.build_browser_csp()
    assert bs._FIREBASE_SDK_ORIGIN in candidate["script-src"]
    for origin in bs._AUTH_API_ORIGINS:
        assert origin in candidate["connect-src"], origin
    storage_origins = bs._storage_connect_origins()
    assert storage_origins, "expected at least one signed-upload storage origin"
    for origin in storage_origins:
        assert origin in candidate["connect-src"], origin


def test_candidate_trusts_no_public_package_cdn():
    # ADR-036: jsDelivr/unpkg are self-hosted, so they must not be script/style
    # authorities. This is the codex-review guard against a promoted policy that
    # remains bypassable via attacker-published CDN packages.
    candidate = bs.build_browser_csp()
    banned = ("jsdelivr", "unpkg", "cdnjs")
    for directive in ("script-src", "style-src"):
        for value in candidate[directive]:
            text = str(value).lower()
            assert not any(host in text for host in banned), (directive, text)


def test_report_only_mode_selection():
    candidate = bs.build_browser_csp()
    enforce, report_only = bs.csp_settings_for_mode("report-only", candidate)
    assert enforce == {}
    assert report_only == candidate


def test_enforce_mode_selection():
    candidate = bs.build_browser_csp()
    enforce, report_only = bs.csp_settings_for_mode("enforce", candidate)
    assert enforce == candidate
    assert report_only == {}


def test_resolve_mode_is_case_insensitive_and_trimmed():
    assert bs.resolve_csp_mode("  ENFORCE ") == "enforce"
    assert bs.resolve_csp_mode("report-only") == "report-only"


def test_invalid_mode_raises_improperly_configured():
    with pytest.raises(ImproperlyConfigured):
        bs.resolve_csp_mode("audit")


def test_default_mode_is_enforce(monkeypatch):
    monkeypatch.delenv("BROWSER_CSP_MODE", raising=False)
    assert bs.configured_csp_mode() == "enforce"
    assert bs.BROWSER_CSP_MODE == "enforce"
    assert bs.build_browser_csp() == bs.SECURE_CSP
    assert bs.SECURE_CSP_REPORT_ONLY == {}


def test_report_only_remains_an_explicit_degraded_mode(monkeypatch):
    monkeypatch.setenv("BROWSER_CSP_MODE", "report-only")
    assert bs.configured_csp_mode() == "report-only"


def test_referrer_policy_is_explicit_same_origin():
    assert bs.SECURE_REFERRER_POLICY == "same-origin"


def test_permissions_policy_denies_capabilities_but_keeps_clipboard():
    for feature in ("camera", "microphone", "geolocation", "payment", "usb"):
        assert f"{feature}=()" in bs.PERMISSIONS_POLICY
    assert "clipboard" not in bs.PERMISSIONS_POLICY


def test_reporting_endpoints_header_points_at_collector():
    assert bs.REPORTING_ENDPOINTS_HEADER == 'csp="/security/csp-report/"'


@pytest.mark.django_db
def test_legacy_response_carries_enforced_headers_without_a_nonce():
    resp = Client().get("/privacy/")
    assert resp.status_code == 200
    policy = resp.headers["Content-Security-Policy"]
    assert "default-src 'none'" in policy
    assert "Content-Security-Policy-Report-Only" not in resp.headers
    # A page that does not read the nonce sends none (Django lazy nonce).
    assert "'nonce-" not in policy
    assert resp.headers["Referrer-Policy"] == "same-origin"
    assert resp.headers["Permissions-Policy"] == bs.PERMISSIONS_POLICY
    assert resp.headers["Reporting-Endpoints"] == bs.REPORTING_ENDPOINTS_HEADER


@pytest.mark.django_db
def test_spa_host_sends_its_template_nonce_and_is_not_cacheable(django_user_model):
    user = django_user_model.objects.create_user(username="op", email="op@example.com", password="pw", is_staff=True)
    client = Client()
    client.force_login(user)
    resp = client.get("/")
    assert resp.status_code == 200
    policy = resp.headers["Content-Security-Policy"]
    assert "Content-Security-Policy-Report-Only" not in resp.headers
    meta = re.search(r'<meta property="csp-nonce" nonce="([^"]+)"', resp.content.decode())
    assert meta, "SPA host must expose the per-response nonce"
    nonce = meta.group(1)
    style_src = next(d for d in policy.split(";") if d.strip().startswith("style-src "))
    script_src = next(d for d in policy.split(";") if d.strip().startswith("script-src "))
    assert f"'nonce-{nonce}'" in style_src.split()
    assert "'nonce-" not in script_src
    assert "no-store" in resp.headers["Cache-Control"]
    assert resp.headers["Referrer-Policy"] == "same-origin"
    assert resp.headers["Permissions-Policy"] == bs.PERMISSIONS_POLICY


@pytest.mark.django_db
def test_spa_host_nonce_changes_per_response(django_user_model):
    user = django_user_model.objects.create_user(username="op2", email="op2@example.com", password="pw")
    client = Client()
    client.force_login(user)
    first = client.get("/").headers["Content-Security-Policy"]
    second = client.get("/").headers["Content-Security-Policy"]
    assert first != second
