"""Best-effort feature acquisition after uniform catalog registration commits."""

import logging

from shared.log_sanitize import safe_log_value

logger = logging.getLogger(__name__)


def _request_feature_artifacts(scenario_id: str) -> None:
    """Acquire the pack's declared feature artifacts before any range needs them (ADR-034-R12).

    Best effort: registration never fails because acquisition could not be
    claimed. A launch that still finds an artifact unavailable fails only that
    range.
    """
    from cms.raes.feature_artifacts import request_pack_feature_artifacts

    try:
        request_pack_feature_artifacts(scenario_id)
    except Exception as exc:  # noqa: BLE001  # best-effort acquisition; log only the failure class
        logger.warning(
            "register_pack: feature artifact acquisition not requested scenario_id=%s error=%s",
            safe_log_value(scenario_id),
            type(exc).__name__,
        )
