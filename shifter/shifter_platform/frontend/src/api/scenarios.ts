/**
 * TanStack Query hooks for the Scenario Editor API surface (#1371). All caching,
 * invalidation, and retry policy live here; components call these hooks and never
 * fetch directly. The SPA uses the canonical `/api/v1/cms/` DRF routes only — it
 * never touches the legacy `/scenario-editor/` Django form/action URLs.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { apiFetch } from "./client";
import type {
  ContentScopeOption,
  ContentScopePage,
  ScenarioCatalogEntry,
  ScenarioDetail,
  ScenarioMetadataState,
  ScenarioMetadataUpdate,
  ScenarioRealizability,
} from "./types";

const BASE = "/cms/scenarios";

export const scenarioKeys = {
  all: ["scenarios"] as const,
  catalog: (scope: ContentScopeOption | null) => ["scenarios", "catalog", scope?.scope_kind, scope?.account, scope?.organization, scope?.workspace] as const,
  contentScopes: (offset: number) => ["scenarios", "content-scopes", offset] as const,
  detail: (id: string) => ["scenarios", "detail", id] as const,
  realizability: (id: string) => ["scenarios", "realizability", id] as const,
};

function invalidateScenarios(queryClient: ReturnType<typeof useQueryClient>, scenarioId?: string) {
  queryClient.invalidateQueries({ queryKey: scenarioKeys.all });
  if (scenarioId !== undefined) {
    queryClient.invalidateQueries({ queryKey: scenarioKeys.detail(scenarioId) });
  }
}

export function contentScopeKey(scope: ContentScopeOption): string {
  return JSON.stringify([scope.scope_kind, scope.account, scope.organization, scope.workspace]);
}

export function useContentScopes(offset: number) {
  return useQuery({
    queryKey: scenarioKeys.contentScopes(offset),
    queryFn: ({ signal }) => apiFetch<ContentScopePage>("/cms/content-scopes/", { signal, query: { offset, limit: 50 } }),
  });
}

export function useScenarioCatalog(scope: ContentScopeOption | null) {
  return useQuery({
    queryKey: scenarioKeys.catalog(scope),
    enabled: scope !== null,
    queryFn: ({ signal }) => apiFetch<ScenarioCatalogEntry[]>("/cms/catalog/", {
      signal,
      query: scope ? { scope_kind: scope.scope_kind, account: scope.account, organization: scope.organization, workspace: scope.workspace } : undefined,
    }),
  });
}

/**
 * Backend realizability for one scenario (#1581, ADR-034-R3).
 *
 * The server owns the whole assessment; this only renders it. A non-realizable
 * result is a normal 200 response, so it is data, not a query error. The read is
 * deliberately separate from `useScenario` because it compiles the pack's SDL —
 * keeping it out of the detail query means opening a scenario never pays that
 * cost until the panel asks for it.
 */
export function useScenarioRealizability(scenarioId: string, enabled = true) {
  return useQuery({
    queryKey: scenarioKeys.realizability(scenarioId),
    enabled: enabled && Boolean(scenarioId),
    queryFn: ({ signal }) =>
      apiFetch<ScenarioRealizability>(`${BASE}/${scenarioId}/realizability/`, { signal }),
  });
}

export function useScenario(scenarioId: string, enabled = true) {
  return useQuery({
    queryKey: scenarioKeys.detail(scenarioId),
    enabled: enabled && Boolean(scenarioId),
    queryFn: ({ signal }) => apiFetch<ScenarioDetail>(`${BASE}/${scenarioId}/`, { signal }),
  });
}

export function useUpdateScenarioMetadata(scenarioId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (body: ScenarioMetadataUpdate) =>
      apiFetch<ScenarioMetadataState>(`${BASE}/${scenarioId}/metadata/`, { method: "PATCH", body }),
    onSuccess: () => invalidateScenarios(queryClient, scenarioId),
  });
}
