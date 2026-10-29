"""Mandatory ADR-068 boundary: the broker control plane is unsupported."""

from shared.model_access.catalog import ContractError


class ModelAccessParked(ContractError):
    """A parked broker operation cannot read selector data or publish authority."""

    def __init__(self) -> None:
        super().__init__("runtime.model_access_parked")


def require_model_access_available() -> None:
    """Refuse independent of runtime flags until ADR-068 is explicitly superseded.

    There is no deployment or test setting that can unpark this surface.
    Low-level accounting models and contracts remain available to their CI lane.
    """
    raise ModelAccessParked
