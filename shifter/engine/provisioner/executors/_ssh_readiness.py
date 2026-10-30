"""Authored, value-free categories for guest SSH readiness failures."""

#: Authored categories for a failed readiness probe, matched against the ssh
#: client's own diagnostics in order. Only the category leaves this module.
_PROBE_FAILURE_CATEGORIES: tuple[tuple[tuple[str, ...], str], ...] = (
    (
        ("Host key verification failed", "REMOTE HOST IDENTIFICATION HAS CHANGED", "host key is known"),
        "host key not verifiable",
    ),
    (("Permission denied",), "authentication refused"),
    (("a password is required", "sudo:"), "privilege escalation refused"),
    (("Connection refused",), "connection refused"),
    (("timed out", "TimeoutError"), "connection timed out"),
    (("No route to host",), "no route to host"),
    (("exit=0",), "unexpected probe output"),
)


def _probe_failure_category(detail: str) -> str:
    """Map a probe's raw diagnostic onto one authored category."""
    for markers, category in _PROBE_FAILURE_CATEGORIES:
        if any(marker in detail for marker in markers):
            return category
    return "unavailable"
