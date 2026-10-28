"""CTF egress proxy setting (#2090): a validated ``host:port`` or empty."""

from __future__ import annotations

import pytest
from django.core.exceptions import ImproperlyConfigured

from config._ctf_egress_settings import resolve_ctf_egress_proxy


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("", ""),
        ("  ", ""),
        ("content-egress:3128", "content-egress:3128"),
        (" content-egress:3128 ", "content-egress:3128"),
    ],
)
def test_accepts_empty_or_host_and_port(raw, expected):
    assert resolve_ctf_egress_proxy(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "content-egress",
        "content-egress:",
        ":3128",
        "content-egress:0",
        "content-egress:70000",
        "http://x:3128",
        "a b:3128",
    ],
)
def test_rejects_malformed_values_at_settings_load(raw):
    with pytest.raises(ImproperlyConfigured, match="CTF_EGRESS_PROXY"):
        resolve_ctf_egress_proxy(raw)
