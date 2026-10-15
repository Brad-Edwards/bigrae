"""Existing durable effect families retain explicit initiating/system authority."""

DURABLE_EFFECT_POLICY = {
    "provision": "reauthorize_initiating_principal_and_original_ceiling",
    "retry": "reauthorize_initiating_principal_and_original_ceiling",
    "cancel": "authorized_caller_and_explicit_system_resource_generation",
    "cleanup": "explicit_system_resource_generation",
    "result": "authenticated_workload_and_exact_attempt",
    "plugin": "reauthorize_initiating_principal_and_pinned_worker_binding",
    "preparation": "reauthorize_initiating_principal_and_pinned_worker_binding",
}
