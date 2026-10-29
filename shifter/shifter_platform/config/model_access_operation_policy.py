"""Explicit unavailable boundary for the sharing routes parked by ADR-068."""

from shared.authorization.operations import OperationPolicy

API_OPERATION_POLICY: dict[str, OperationPolicy] = {
    f"model_access_bindings_{operation}_create": OperationPolicy(
        (), None, "parked_sharing_boundary", exemption="parked_feature_unavailable"
    )
    for operation in ("validate", "policy_preview", "selector_preview", "publish", "drain")
}
