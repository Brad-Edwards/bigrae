"""Request schemas for provisioning request tracking.

A Request is an explicit container for items being provisioned.
Items within a request have independent lifecycles.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from .base import SpecBase
from .range import InstanceSpec, RangeSpec

AnyRangeSpec = RangeSpec


class RequestSpec(SpecBase):
    """Specification for a provisioning request.

    A request groups related items that were requested together,
    but each item has its own independent lifecycle.

    Attributes:
        request_id: Unique identifier for this request.
        user_id: User who made the request.
        created_at: When the request was created.
        items: List of specs being requested.
    """

    request_id: UUID
    user_id: int | None
    owner_principal_uuid: UUID | None = None
    created_at: datetime | None = None

    # Items can be range specs or NGFW InstanceSpecs.
    items: list[AnyRangeSpec | InstanceSpec] = Field(default_factory=list)

    @field_validator("user_id")
    @classmethod
    def user_id_positive(cls, v: int | None) -> int | None:
        """Validate user_id is a positive integer."""
        if v is not None and v <= 0:
            raise ValueError("user_id must be a positive integer")
        return v

    @model_validator(mode="after")
    def explicit_owner(self):
        if self.user_id is None and (self.owner_principal_uuid is None or not self.owner_principal_uuid.int):
            raise ValueError("Request requires an explicit owner")
        return self
