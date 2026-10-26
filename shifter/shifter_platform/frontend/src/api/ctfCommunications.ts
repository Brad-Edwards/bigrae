/** Scoped communication writes and bounded organizer reads. */
import { useRef } from "react";
import { useInfiniteQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { apiFetch } from "./client";
import type { components } from "./schema";

const COMMUNICATION_QUERY_KEY = "ctf-communications";

type Campaign = components["schemas"]["CommunicationCampaignSummary"];
type Intent = components["schemas"]["CommunicationIntent"];
type Input = { subject: string; body: string; scheduled_at?: string };
type Pending = { current: { fingerprint: string; campaignId: string } | null };

type CommunicationParent = { account: string } | { organization: string } | { workspace: string };

async function parentForEvent(eventId: string): Promise<CommunicationParent> {
  const event = await apiFetch<components["schemas"]["EventDetail"]>(`/ctf/events/${eventId}/`);
  if (event.workspace) return { workspace: event.workspace };
  if (event.organization) return { organization: event.organization };
  if (event.account) return { account: event.account };
  throw new Error("Communication access to this event scope is unavailable.");
}

export async function submitCommunication(eventId: string, input: Input, pending: Pending) {
  const fingerprint = JSON.stringify([eventId, input]);
  if (pending.current?.fingerprint !== fingerprint) {
    const parent = await parentForEvent(eventId);
    const campaign = await apiFetch<Campaign>("/ctf/communications/", { method: "POST", body: {
      ...parent, title: input.subject, subject: input.subject, body: input.body,
      target_event_ids: [eventId], audience_spec: { kind: "event", event_ids: [eventId] },
      trigger_spec: input.scheduled_at ? { kind: "absolute_time", due_at: input.scheduled_at } : { kind: "manual" },
      channels: ["in_app"], acknowledgement_policy: "none",
    } });
    pending.current = { fingerprint, campaignId: campaign.id };
  }
  const result = await apiFetch<Intent>(`/ctf/communications/${pending.current.campaignId}/release/`, {
    method: "POST", body: { occurrence_key: "initial" },
  });
  pending.current = null;
  return result;
}

export function useAnnounceCtfNotification(eventId: string) {
  const client = useQueryClient();
  const pending = useRef<Pending["current"]>(null);
  return useMutation({
    mutationFn: (input: Input) => submitCommunication(eventId, input, pending),
    onSuccess: () => client.invalidateQueries({ queryKey: [COMMUNICATION_QUERY_KEY, eventId] }),
  });
}

export function useCtfCommunications(eventId: string) {
  return useInfiniteQuery({ queryKey: [COMMUNICATION_QUERY_KEY, eventId], enabled: Boolean(eventId),
    initialPageParam: 0,
    getNextPageParam: (page: components["schemas"]["CampaignList"]) => page.next_offset ?? undefined,
    queryFn: async ({ pageParam }) => {
    const parent = await parentForEvent(eventId);
    const query = new URLSearchParams({ ...parent, event_id: eventId, limit: "25", offset: String(pageParam) });
    return apiFetch<components["schemas"]["CampaignList"]>(
      `/ctf/communications/?${query.toString()}`,
    );
  } });
}

export function useCancelCtfCommunication(eventId: string) {
  const client = useQueryClient();
  return useMutation({ mutationFn: (campaignId: string) => apiFetch<Campaign>(
    `/ctf/communications/${campaignId}/cancel/`, { method: "POST", body: {} },
  ), onSuccess: () => client.invalidateQueries({ queryKey: [COMMUNICATION_QUERY_KEY, eventId] }) });
}
