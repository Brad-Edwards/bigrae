"""A publisher cannot replace the device selected by an authorized socket."""

import json
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest


@pytest.mark.asyncio
async def test_foreign_device_event_does_not_leave_the_subscription():
    from mission_control.status_consumers import NGFWStatusConsumer

    consumer = NGFWStatusConsumer()
    consumer.app_id = str(uuid4())
    consumer.send = AsyncMock()
    await consumer.ngfw_status(
        {
            "type": "ngfw_status",
            "app_id": str(uuid4()),
            "status": "ready",
            "state": {"private_marker": "FOREIGN_DEVICE_MARKER"},
        }
    )
    consumer.send.assert_not_awaited()
    await consumer.ngfw_status({"type": "ngfw_status", "app_id": consumer.app_id, "status": "ready"})
    message = json.loads(consumer.send.call_args.kwargs["text_data"])
    assert message["app_id"] == consumer.app_id
    assert "FOREIGN_DEVICE_MARKER" not in repr(message)
