/**
 * TanStack Query hooks for the retained lease-policy administration API. All
 * caching, invalidation, and retry policy live here; components call these hooks
 * and never fetch directly. Every call goes to the canonical `/api/v1/administer/`
 * DRF surface — the authoritative permission and audit boundary.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { apiFetch } from "./client";
import type {
  MissionControlLeasePolicySettings,
  ReplaceMissionControlLeasePolicyRequest,
  ResetMissionControlLeasePolicyRequest,
} from "./types";

export const missionControlLeasePolicyKeys = {
  all: ["administer", "mission-control-lease-policy"] as const,
  settings: () => ["administer", "mission-control-lease-policy", "settings"] as const,
};

function invalidateMissionControlLeasePolicy(queryClient: ReturnType<typeof useQueryClient>) {
  queryClient.invalidateQueries({ queryKey: missionControlLeasePolicyKeys.all });
}
export function useMissionControlLeasePolicySettings() {
  return useQuery({
    queryKey: missionControlLeasePolicyKeys.settings(),
    queryFn: ({ signal }) =>
      apiFetch<MissionControlLeasePolicySettings>("/administer/mission-control/lease-policy/", { signal }),
  });
}
export function useReplaceTenantLeasePolicy() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (body: ReplaceMissionControlLeasePolicyRequest) =>
      apiFetch<MissionControlLeasePolicySettings>("/administer/mission-control/lease-policy/tenant/", {
        method: "PUT",
        body,
      }),
    retry: false,
    onSuccess: () => invalidateMissionControlLeasePolicy(queryClient),
  });
}

export function useResetTenantLeasePolicy() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (body: ResetMissionControlLeasePolicyRequest) =>
      apiFetch<MissionControlLeasePolicySettings>("/administer/mission-control/lease-policy/tenant/reset/", {
        method: "POST",
        body,
      }),
    retry: false,
    onSuccess: () => invalidateMissionControlLeasePolicy(queryClient),
  });
}

export function useReplaceGroupLeasePolicy(groupId: number) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (body: ReplaceMissionControlLeasePolicyRequest) =>
      apiFetch<MissionControlLeasePolicySettings>(`/administer/mission-control/lease-policy/groups/${groupId}/`, {
        method: "PUT",
        body,
      }),
    retry: false,
    onSuccess: () => invalidateMissionControlLeasePolicy(queryClient),
  });
}

export function useResetGroupLeasePolicy(groupId: number) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (body: ResetMissionControlLeasePolicyRequest) =>
      apiFetch<MissionControlLeasePolicySettings>(
        `/administer/mission-control/lease-policy/groups/${groupId}/reset/`,
        { method: "POST", body },
      ),
    retry: false,
    onSuccess: () => invalidateMissionControlLeasePolicy(queryClient),
  });
}
