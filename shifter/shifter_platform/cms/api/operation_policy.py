"""S6 bindings for canonical CMS operations; S8 activates owning service checks."""

from shared.authorization.operations import OperationPolicy, scope_actions

_READ = scope_actions("read_content", installation=True)
_WRITE = scope_actions("manage_content", installation=True)
_PREPARE = scope_actions("prepare_content", installation=True)
_MODELS = ("organization.manage_model_sources",)
_ADAPTERS = ("organization.manage_adapters",)

API_OPERATION_POLICY: dict[str, OperationPolicy] = {
    "cms_artifact_preparation_create": OperationPolicy(
        _PREPARE, "scope", "preparation_parent", selector="validated_parent"
    ),
    "cms_artifact_preparation_retrieve": OperationPolicy(_PREPARE, "scope", "preparation_operation"),
    "cms_artifact_preparation_cancel_create": OperationPolicy(_PREPARE, "scope", "preparation_operation"),
    "cms_artifact_preparation_retry_create": OperationPolicy(_PREPARE, "scope", "preparation_operation"),
    "cms_catalog_list": OperationPolicy(
        _READ, "scope", "catalog_collection", selector="private_scope_or_explicit_publication"
    ),
    "cms_catalog_retrieve": OperationPolicy(_READ, "scope", "catalog_entry", selector="persisted_source_scope"),
    "cms_catalog_packs_create": OperationPolicy(_WRITE, "scope", "content_parent", selector="validated_parent"),
    "cms_model_source_options_retrieve": OperationPolicy(_READ, "scope", "content_parent", selector="validated_parent"),
    "cms_organizations_model_ranges_retrieve": OperationPolicy(
        _MODELS, "organization", "organization_range_collection"
    ),
    "cms_organizations_model_source_users_retrieve": OperationPolicy(
        _MODELS, "organization", "organization_source_collection"
    ),
    "cms_organizations_model_sources_retrieve": OperationPolicy(
        _MODELS, "organization", "organization_source_collection"
    ),
    "cms_organizations_model_sources_create": OperationPolicy(_MODELS, "organization", "organization"),
    "cms_organizations_model_sources_update": OperationPolicy(_MODELS, "organization", "model_source"),
    "cms_organizations_model_sources_retire_credentials_create": OperationPolicy(
        _MODELS, "organization", "model_source"
    ),
    "cms_organizations_model_sources_available_retrieve": OperationPolicy(
        _MODELS, "organization", "organization_source_collection"
    ),
    "cms_organizations_packs_create": OperationPolicy(("organization.manage_content",), "organization", "organization"),
    "cms_organizations_packs_removal_create": OperationPolicy(
        ("organization.manage_content",), "organization", "organization_pack", selector="persisted_source_scope"
    ),
    "cms_organizations_plugin_packs_list": OperationPolicy(_ADAPTERS, "organization", "organization_plugin_collection"),
    "cms_organizations_plugin_packs_retrieve": OperationPolicy(_ADAPTERS, "organization", "plugin_pack"),
    "cms_organizations_plugin_packs_create": OperationPolicy(_ADAPTERS, "organization", "plugin_pack"),
    "cms_organizations_plugins_retrieve": OperationPolicy(_ADAPTERS, "organization", "organization_plugin_collection"),
    "cms_organizations_plugins_create": OperationPolicy(_ADAPTERS, "organization", "organization"),
    "cms_organizations_plugins_actions_create": OperationPolicy(_ADAPTERS, "organization", "plugin_installation"),
    "cms_preparation_adapter_grants_list": OperationPolicy(
        ("installation.manage_adapters",), "installation", "adapter_grants"
    ),
    "cms_preparation_adapters_list": OperationPolicy(
        ("installation.manage_adapters",), "installation", "adapter_catalog"
    ),
    "cms_preparation_adapters_create": OperationPolicy(
        ("installation.manage_adapters",), "installation", "adapter_catalog"
    ),
    "cms_preparation_adapters_state_create": OperationPolicy(
        ("installation.manage_adapters",), "installation", "adapter"
    ),
    "cms_raes_image_mappings_list": OperationPolicy(("installation.prepare_content",), "installation", "image_catalog"),
    "cms_raes_image_mappings_create": OperationPolicy(
        ("installation.prepare_content",), "installation", "image_catalog"
    ),
    "cms_raes_image_mappings_disable_create": OperationPolicy(
        ("installation.prepare_content",), "installation", "image_mapping"
    ),
    "cms_ranges_model_sources_retrieve": OperationPolicy(("range.read",), "range", "range_request"),
    "cms_ranges_model_sources_update": OperationPolicy(("range.manage",), "range", "range_request"),
    "cms_ranges_model_sources_revoke_create": OperationPolicy(("range.manage",), "range", "range_request"),
    "api_v1_cms_range_workspace_rebind": OperationPolicy(
        scope_actions("manage_range_scope"), "scope", "range_source_and_destination", selector="both_scopes"
    ),
    "cms_scenarios_retrieve": OperationPolicy(_READ, "scope", "catalog_entry", selector="persisted_source_scope"),
    "cms_scenarios_metadata_partial_update": OperationPolicy(
        _WRITE, "scope", "catalog_entry", selector="persisted_source_scope"
    ),
    "cms_scenarios_realizability_retrieve": OperationPolicy(
        _READ, "scope", "catalog_entry", selector="persisted_source_scope"
    ),
    "api_v1_cms_workspace_range_scope_list": OperationPolicy(
        ("workspace.manage_range_scope",), "workspace", "workspace_range_collection"
    ),
}

EXTERNAL_EFFECT_POLICY = {
    "preparation_worker_input": "authenticated_workload_and_live_initiating_principal",
    "preparation_worker_result": "authenticated_workload_and_exact_attempt_generation",
    "vpn_connect": "authenticated_workload_and_live_principal_range_authority",
    "vpn_heartbeat": "authenticated_workload_and_live_principal_range_authority",
    "vpn_end": "authenticated_workload_and_exact_session",
}
