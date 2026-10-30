"""CTF Notification service.

Business logic for email notifications and the real-time event bus. The
implementation is split across private submodules (``_participant``,
``_organizer``, ``_email``, ``_cleanup``, ``_scheduled``,
``delivery_milestones``, ``realtime``) and re-exported here so callers keep
using ``from ctf.services.notification import X`` / ``from ctf.services
import notification``.

PATCH LOCALITY: submodules resolve the email helpers (``_send_email`` /
``_render_email``) and models through this package at call time
(``from ctf.services import notification as _n``), so a
``patch("ctf.services.notification.<name>")`` mutates the single attribute
those submodules look up when they run.
"""

from __future__ import annotations

# --- Names tests patch via ``patch("ctf.services.notification.X")`` --------
from ctf.models import CTFEvent, CTFNotification, CTFParticipant

from ._cleanup import send_cleanup_warning
from ._email import _build_ctf_login_url, _render_email, _send_email
from ._organizer import (
    ALERT_BODIES,
    ALERT_KINDS,
    OrganizerAlert,
    alert_for,
    alert_if_ranges_not_ready,
    declare_or_retain_lifecycle_alert,
    declare_organizer_alert,
    notify_organizer_capacity_outcome,
    notify_organizer_provision_failure,
    redeclare_pending_alerts,
)
from ._participant import (
    send_credentials,
    send_login_info,
    send_reminder,
)
from ._scheduled import (
    _deliver_announcement,
    cancel_scheduled_notification,
    deliver_scheduled_notification,
    schedule_notification,
    send_announcement,
)
from .delivery_milestones import (
    notify_participant_provision_failure,
    send_event_results,
    send_range_ready,
)
from .realtime import (
    publish_event_notification,
    register_ctf_notifications,
)

__all__ = (
    "ALERT_BODIES",
    "ALERT_KINDS",
    "CTFEvent",
    "CTFNotification",
    "CTFParticipant",
    "OrganizerAlert",
    "_build_ctf_login_url",
    "_deliver_announcement",
    "_render_email",
    "_send_email",
    "alert_for",
    "alert_if_ranges_not_ready",
    "cancel_scheduled_notification",
    "declare_or_retain_lifecycle_alert",
    "declare_organizer_alert",
    "deliver_scheduled_notification",
    "notify_organizer_capacity_outcome",
    "notify_organizer_provision_failure",
    "notify_participant_provision_failure",
    "publish_event_notification",
    "redeclare_pending_alerts",
    "register_ctf_notifications",
    "schedule_notification",
    "send_announcement",
    "send_cleanup_warning",
    "send_credentials",
    "send_event_results",
    "send_login_info",
    "send_range_ready",
    "send_reminder",
)
