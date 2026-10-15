"""Principal-native catalog branch; explicit publication never exposes sources."""

from django.db.models import Q

from cms.models import RaesPackageSource, ScenarioMetadata
from cms.services._content_authority import content_scope, content_scope_fields, require_content_scope
from shared.credentials import CredentialContext
from shared.identity_scope import ResourceScope
from shared.schemas.cms_projections import ScenarioProjection


def list_scoped_scenarios(credential: CredentialContext, scope: ResourceScope, *, include_unavailable: bool):
    """Filter before metadata lookup, projection, sorting, counts or cache use."""
    from .registry import _raes_launchable, _raes_source_to_dict

    require_content_scope(credential, scope, "read_content")
    shared = Q(
        scope_kind="installation",
        account_uuid__isnull=True,
        organization_uuid__isnull=True,
        workspace_uuid__isnull=True,
        owner_principal_uuid__isnull=False,
        is_operator_published=True,
    )
    private = Q(**content_scope_fields(scope), owner_principal_uuid__isnull=False)
    sources = tuple(RaesPackageSource.objects.filter(private | shared).order_by("scenario_id"))
    metadata = {
        row.scenario_id: {"enabled": row.enabled, "staff_only": row.staff_only}
        for row in ScenarioMetadata.objects.filter(scenario_id__in=[row.scenario_id for row in sources])
    }
    entries: list[ScenarioProjection] = []
    for source in sources:
        content_scope(source)
        overlay = metadata.get(source.scenario_id)
        if include_unavailable or overlay is None or overlay["enabled"]:
            # The incumbent allowlist excludes package_ref, provenance, source
            # credentials, recipes, topology and preparation evidence.
            entries.append(_raes_source_to_dict(source, metadata=overlay, launchable=_raes_launchable(source)))
    entries.sort(key=lambda entry: entry["name"])
    return entries
