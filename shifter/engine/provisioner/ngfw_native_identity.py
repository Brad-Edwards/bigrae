"""Stable native device naming without assigning a human to service work."""

from uuid import UUID


def native_owner_key(user_id: int | None, instance_id: str) -> str:
    """Preserve deployed human names; bind service names to the native device."""
    if user_id is None:
        return f"device-{UUID(instance_id)}"
    if isinstance(user_id, bool) or not isinstance(user_id, int) or user_id <= 0:
        raise ValueError("Invalid native device owner")
    return f"user-{user_id}"
