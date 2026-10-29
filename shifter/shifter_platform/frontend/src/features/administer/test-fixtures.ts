import type { AuditLog } from "@/api/types";

export function auditEvent(overrides: Partial<AuditLog> = {}): AuditLog {
  return {
    id: 1,
    entity_type: "workspace_membership",
    entity_id: 42,
    action: "role_sync",
    actor_type: "user",
    actor_principal_uuid: null,
    actor_id: 5,
    timestamp: "2026-08-01T12:00:00Z",
    previous_state: null,
    new_state: null,
    context: "",
    source_ip: null,
    user_agent: "",
    request_id: "",
    ...overrides,
  };
}

export function pageOf<T>(results: T[]): { count: number; next: null; previous: null; results: T[] } {
  return { count: results.length, next: null, previous: null, results };
}
