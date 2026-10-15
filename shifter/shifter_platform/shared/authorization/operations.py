"""Explicit operation bindings; locators describe owning SQL service seams."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class OperationPolicy:
    """One operation's exact actions and trusted resource locator."""

    actions: tuple[str, ...]
    target: str | None
    locator: str | None
    exemption: str | None = None
    selector: str | None = None


def scope_actions(operation: str, *, installation: bool = False) -> tuple[str, ...]:
    """Enumerate concrete registered parents, never an effective wildcard grant."""
    parents = (
        ("installation", "account", "organization", "workspace")
        if installation
        else ("account", "organization", "workspace")
    )
    return tuple(f"{parent}.{operation}" for parent in parents)
