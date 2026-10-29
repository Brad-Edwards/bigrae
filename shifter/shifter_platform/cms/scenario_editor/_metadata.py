"""ScenarioMetadata service operations."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from cms.models import ScenarioMetadata
from cms.scenarios.realizability import get_scenario_realizability
from shared.credentials import CredentialContext
from shared.identity_scope import ResourceScope
from shared.log_sanitize import safe_log_value
from shared.raes.realizability import RealizabilityOutcome

from ._common import ScenarioEditorError

if TYPE_CHECKING:
    from django.contrib.auth.models import User

logger = logging.getLogger(__name__)

#: Cap on gaps quoted in a refusal so the message stays readable and bounded.
_MAX_REPORTED_GAPS = 3


#: Outcomes that permit publication. ``INDETERMINATE`` is deliberately absent:
#: an assessment that could not be completed is not permission to publish.
_PUBLISHABLE_OUTCOMES = frozenset({RealizabilityOutcome.REALIZABLE, RealizabilityOutcome.NOT_APPLICABLE})

#: Reported when no assessment could be produced at all. Deliberately reuses the
#: INDETERMINATE wording: from the author's side, an entry that cannot be
#: assessed and one whose assessment was inconclusive are the same refusal.
_UNASSESSED_OUTCOME = RealizabilityOutcome.INDETERMINATE


def _assert_publishable(
    scenario_id: str,
    *,
    enabled: bool | None,
    credential: CredentialContext | None = None,
    scope: ResourceScope | None = None,
) -> None:
    """Refuse to enable an RAES entry the backend cannot realize (ADR-034-R3).

    This is the authoritative gate. The editor's badge is advisory -- a caller
    can ignore it, drive the API directly, or race a registry change -- so
    realizability is recomputed here, at the boundary that actually flips the
    desired state, and never trusted from the request.

    Only publication is gated: disabling, staff-only toggles, and saving a
    non-realizable pack for staff review all remain allowed.

    A missing assessment (``None``) is a refusal, not a pass. Existence
    verification and assessment are separate lookups, so an entry that vanishes
    or resolves inconsistently between them must fail closed -- "no assessment"
    is not "nothing to assess", and only an explicit ``NOT_APPLICABLE`` result
    means realizability genuinely does not apply.
    """
    if enabled is not True:
        return

    assessment = (
        get_scenario_realizability(scenario_id, credential=credential, scope=scope)
        if credential is not None
        else get_scenario_realizability(scenario_id)
    )
    if assessment is not None and assessment["outcome"] in _PUBLISHABLE_OUTCOMES:
        return

    outcome = assessment["outcome"] if assessment else _UNASSESSED_OUTCOME
    gaps = assessment["gaps"] if assessment else []
    logger.warning(
        "publication refused: scenario_id=%s outcome=%s gap_codes=%s",
        safe_log_value(scenario_id),
        outcome,
        sorted({gap["code"] for gap in gaps}),
    )
    raise ScenarioEditorError(_refusal_message(outcome, gaps))


def _refusal_message(outcome: str, gaps: list[dict[str, str]]) -> str:
    """Explain the refusal using the same bounded gaps the editor renders."""
    detail = "; ".join(f"{gap['address']}: {gap['message']}" for gap in gaps[:_MAX_REPORTED_GAPS])
    if outcome == RealizabilityOutcome.INDETERMINATE:
        lead = "Cannot confirm this scenario is realizable by the selected backend, so it cannot be enabled"
    else:
        lead = "The selected backend cannot realize this scenario, so it cannot be enabled"
    return f"{lead}. {detail}" if detail else f"{lead}."


def update_metadata(
    user: User | CredentialContext,
    scenario_id: str,
    *,
    enabled: bool | None = None,
    staff_only: bool | None = None,
) -> ScenarioMetadata:
    """Mutate availability only under an admitted native credential and source lock."""
    if not isinstance(user, CredentialContext):
        raise ScenarioEditorError("Scenario metadata authority unavailable")
    from ._scoped_metadata import update_scoped_metadata

    return update_scoped_metadata(user, scenario_id, enabled=enabled, staff_only=staff_only)
