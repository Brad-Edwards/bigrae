"""ECS-compliant logging configuration for the OpenVPN pool server.

Matches the ECS format of the Django platform and the provisioner, so every
service emits one log schema.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import UTC, datetime

ECS_VERSION = "8.11"


class ECSFormatter(logging.Formatter):
    """Format logs as ECS-compliant JSON."""

    def format(self, record: logging.LogRecord) -> str:
        log_obj: dict[str, object] = {
            "@timestamp": datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "log.level": record.levelname,
            "message": record.getMessage(),
            "ecs.version": ECS_VERSION,
            "service.name": "openvpn",
            "service.environment": os.environ.get("ENVIRONMENT", "unknown"),
            "log.logger": record.name,
            "log.origin.function": record.funcName,
            "log.origin.file.line": record.lineno,
        }
        if record.exc_info:
            log_obj["error.stack_trace"] = self.formatException(record.exc_info)
        return json.dumps(log_obj, default=str)


def configure_logging(level: int = logging.INFO) -> None:
    """Configure the root logger with ECS formatting on the container's stdout."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(ECSFormatter())
    logging.root.handlers = [handler]
    logging.root.setLevel(level)
