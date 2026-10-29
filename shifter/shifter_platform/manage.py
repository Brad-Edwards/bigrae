#!/usr/bin/env python
"""Django's command-line utility for administrative tasks."""

import logging
import os
import sys
from pathlib import Path
from types import TracebackType

# Add shifter/ to the path for sibling runtime packages.
SHIFTER_DIR = Path(__file__).resolve().parent.parent
if str(SHIFTER_DIR) not in sys.path:
    sys.path.insert(0, str(SHIFTER_DIR))

# Named explicitly: under ``python manage.py`` this module's ``__name__`` is ``__main__``.
logger = logging.getLogger("manage")


def log_uncaught_exception(exc_type: type[BaseException], exc: BaseException, traceback: TracebackType | None) -> None:
    """Send an uncaught command exception through the configured logging.

    The platform's workers and Jobs run as management commands. Without this hook
    an uncaught exception reaches only unstructured stderr, outside the ECS log
    pipeline. The process still exits non-zero.
    """
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc, traceback)
        return
    logger.critical("Management command failed with an unhandled exception", exc_info=(exc_type, exc, traceback))


def main() -> None:
    """Run administrative tasks."""
    sys.excepthook = log_uncaught_exception
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    try:
        from django.core.management import execute_from_command_line
    except ImportError as exc:
        raise ImportError(
            "Couldn't import Django. Are you sure it's installed and "
            "available on your PYTHONPATH environment variable? Did you "
            "forget to activate a virtual environment?"
        ) from exc
    execute_from_command_line(sys.argv)


if __name__ == "__main__":
    main()
