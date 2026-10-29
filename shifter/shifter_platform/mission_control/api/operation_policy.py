"""S6 Mission Control actions and exact owning-service locators."""

from shared.authorization.operations import OperationPolicy, scope_actions

_READ = ("range.read",)
_MANAGE = ("range.manage",)
_ACCESS = ("range.access",)
_DEVICE_ACCESS = scope_actions("access_devices")

API_OPERATION_POLICY: dict[str, OperationPolicy] = {
    "api_v1_mission_control_agents_list": OperationPolicy(scope_actions("read_content"), "scope", "asset_collection"),
    "api_v1_mission_control_credentials_create": OperationPolicy(
        scope_actions("manage_credentials"), "scope", "credential_parent"
    ),
    "api_v1_mission_control_credentials_delete": OperationPolicy(
        scope_actions("manage_credentials"), "scope", "credential"
    ),
    "api_v1_mission_control_guacamole_bootstrap_status_retrieve": OperationPolicy(
        _ACCESS + _DEVICE_ACCESS, "resource", "bootstrap", selector="persisted_protocol_and_target"
    ),
    "api_v1_mission_control_guacamole_bootstrap_open_retrieve": OperationPolicy(
        _ACCESS + _DEVICE_ACCESS, "resource", "bootstrap", selector="persisted_protocol_and_target"
    ),
    "api_v1_mission_control_guacamole_rdp_url": OperationPolicy(_ACCESS, "range", "range_instance"),
    "api_v1_mission_control_guacamole_ssh_url": OperationPolicy(_ACCESS, "range", "range_instance"),
    "api_v1_mission_control_ngfw_create": OperationPolicy(scope_actions("manage_devices"), "scope", "device_parent"),
    "api_v1_mission_control_ngfw_destroy": OperationPolicy(scope_actions("manage_devices"), "scope", "device"),
    "api_v1_mission_control_ngfw_ssh_url": OperationPolicy(_DEVICE_ACCESS, "scope", "device"),
    "api_v1_mission_control_ngfw_list": OperationPolicy(scope_actions("read_devices"), "scope", "device_collection"),
    "api_v1_mission_control_range_retrieve": OperationPolicy(_READ, "range", "current_range"),
    "api_v1_mission_control_range_cleanup_outcome": OperationPolicy(_READ, "range", "range_request"),
    "api_v1_mission_control_raes_operation_receipts_list": OperationPolicy(_READ, "range", "range_request"),
    "api_v1_mission_control_raes_operation_status_list": OperationPolicy(_READ, "range", "range_request"),
    "api_v1_mission_control_raes_participant_implementations_list": OperationPolicy(_READ, "range", "range_request"),
    "api_v1_mission_control_raes_participant_runtimes_list": OperationPolicy(_READ, "range", "range_request"),
    "api_v1_mission_control_raes_runtime_snapshots_list": OperationPolicy(_READ, "range", "range_request"),
    "api_v1_mission_control_range_cancel": OperationPolicy(_MANAGE, "range", "current_range"),
    "api_v1_mission_control_range_destroy": OperationPolicy(_MANAGE, "range", "current_range"),
    "mission_control_range_extend_create": OperationPolicy(_MANAGE, "range", "current_range"),
    "api_v1_mission_control_range_launch": OperationPolicy(
        scope_actions("launch_range"), "scope", "launch_parent", selector="validated_parent"
    ),
    "api_v1_mission_control_launch_workspaces_list": OperationPolicy(
        ("workspace.launch_range",), "workspace", "authorized_workspace_collection"
    ),
    "api_v1_mission_control_range_pause": OperationPolicy(_MANAGE, "range", "current_range"),
    "api_v1_mission_control_range_resume": OperationPolicy(_MANAGE, "range", "current_range"),
    "mission_control_range_vpn_profile_create": OperationPolicy(_ACCESS, "range", "current_range"),
    "api_v1_mission_control_ranges_list": OperationPolicy(_READ, "range", "range_collection"),
    "api_v1_mission_control_scenarios_list": OperationPolicy(
        scope_actions("read_content", installation=True),
        "scope",
        "catalog_collection",
        selector="private_scope_or_explicit_publication",
    ),
    "api_v1_mission_control_upload_cancel": OperationPolicy(scope_actions("manage_content"), "scope", "upload_intent"),
    "api_v1_mission_control_upload_complete": OperationPolicy(
        scope_actions("manage_content"), "scope", "upload_intent"
    ),
    "api_v1_mission_control_upload_initiate": OperationPolicy(
        scope_actions("manage_content"), "scope", "upload_parent"
    ),
}

CHANNEL_POLICY = {
    "ssh": OperationPolicy(
        _ACCESS + _DEVICE_ACCESS, "resource", "realized_instance", selector="persisted_resource_kind"
    ),
    "range_status": OperationPolicy(_READ, "range", "range_request"),
    "ngfw_status": OperationPolicy(scope_actions("read_devices"), "scope", "device"),
    "guacamole": OperationPolicy(
        _ACCESS + _DEVICE_ACCESS, "resource", "bootstrap", selector="persisted_protocol_and_target"
    ),
    "vpn": OperationPolicy(_ACCESS, "range", "certificate_generation"),
}
