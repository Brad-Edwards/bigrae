/** Scoped communication writes and bounded organizer reads. */
import { useRef } from "react";
import { keepPreviousData, useInfiniteQuery, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiFetch } from "./client";
import type { components } from "./schema";

const COMMUNICATION_QUERY_KEY = "ctf-communications";

export type Campaign = components["schemas"]["CommunicationCampaignSummary"];
type Intent = components["schemas"]["CommunicationIntent"];
type CampaignList = components["schemas"]["CampaignList"];
export type CommunicationTargets = components["schemas"]["CommunicationTargets"];
export type CommunicationPreview = components["schemas"]["CommunicationPreview"];
export type DeliveryReport = components["schemas"]["CommunicationDeliveryReport"];
export type RecipientPage = components["schemas"]["CommunicationRecipientPage"];
export type CommunicationActivity = components["schemas"]["CommunicationActivity"];
export type AcknowledgementPolicy = "none" | "read" | "explicit";

/** One draft for one or more events in one workspace; the single-event dialog is a one-target draft. */
export type DraftInput = {
  workspaceId: string;
  eventIds: string[];
  title: string;
  subject: string;
  body: string;
  channels?: ("in_app" | "email")[];
  acknowledgementPolicy?: AcknowledgementPolicy;
  /** UTC ISO instant for a scheduled send; omitted for send now. */
  dueAt?: string;
};

type Input = { subject: string; body: string; scheduled_at?: string };
type Pending = { current: { fingerprint: string; campaignId: string; revisionId?: string } | null };

const keys = {
  all: [COMMUNICATION_QUERY_KEY] as const,
  event: (eventId: string) => [COMMUNICATION_QUERY_KEY, eventId] as const,
  targets: (workspaceId: string) => [COMMUNICATION_QUERY_KEY, "targets", workspaceId] as const,
  workspace: (workspaceId: string) => [COMMUNICATION_QUERY_KEY, "workspace", workspaceId] as const,
  campaign: (campaignId: string) => [COMMUNICATION_QUERY_KEY, "campaign", campaignId] as const,
  preview: (campaignId: string, revisionId: string) =>
    [COMMUNICATION_QUERY_KEY, "campaign", campaignId, "preview", revisionId] as const,
};

export function draftBody(input: DraftInput) {
  const [first] = input.eventIds;
  return {
    workspace_id: input.workspaceId,
    title: input.title,
    subject: input.subject,
    body: input.body,
    target_event_ids: input.eventIds,
    audience_spec:
      input.eventIds.length > 1
        ? { kind: "multi_event" as const, event_ids: input.eventIds }
        : { kind: "event" as const, event_ids: [first] },
    trigger_spec: input.dueAt ? { kind: "absolute_time" as const, due_at: input.dueAt } : { kind: "manual" as const },
    channels: input.channels ?? ["in_app"],
    acknowledgement_policy: input.acknowledgementPolicy ?? "none",
  };
}

export function createDraft(input: DraftInput) {
  return apiFetch<Campaign>("/ctf/communications/", { method: "POST", body: draftBody(input) });
}

export function reviseDraft(campaignId: string, content: { subject: string; body: string }) {
  return apiFetch<Campaign>(`/ctf/communications/${campaignId}/revisions/`, { method: "POST", body: content });
}

/** Release the reviewed revision; a retry reuses the same campaign and occurrence. */
export function releaseCommunication(campaignId: string, revisionId?: string, occurrenceKey = "initial") {
  return apiFetch<Intent>(`/ctf/communications/${campaignId}/release/`, {
    method: "POST",
    body: revisionId ? { occurrence_key: occurrenceKey, revision_id: revisionId } : { occurrence_key: occurrenceKey },
  });
}

async function workspaceForEvent(eventId: string) {
  const event = await apiFetch<components["schemas"]["EventDetail"]>(`/ctf/events/${eventId}/`);
  if (!event.workspace) throw new Error("Communication access to this workspace is unavailable.");
  return event.workspace;
}

export async function submitCommunication(eventId: string, input: Input, pending: Pending) {
  const fingerprint = JSON.stringify([eventId, input]);
  if (pending.current?.fingerprint !== fingerprint) {
    const campaign = await createDraft({
      workspaceId: await workspaceForEvent(eventId),
      eventIds: [eventId],
      title: input.subject,
      subject: input.subject,
      body: input.body,
      dueAt: input.scheduled_at,
    });
    pending.current = { fingerprint, campaignId: campaign.id, revisionId: campaign.latest_revision?.id };
  }
  const result = await releaseCommunication(pending.current.campaignId, pending.current.revisionId);
  pending.current = null;
  return result;
}

export function useAnnounceCtfNotification(eventId: string) {
  const client = useQueryClient();
  const pending = useRef<Pending["current"]>(null);
  return useMutation({
    mutationFn: (input: Input) => submitCommunication(eventId, input, pending),
    onSuccess: () => client.invalidateQueries({ queryKey: keys.all }),
  });
}

export function useCtfCommunications(eventId: string) {
  return useInfiniteQuery({ queryKey: keys.event(eventId), enabled: Boolean(eventId),
    initialPageParam: 0,
    getNextPageParam: (page: CampaignList) => page.next_offset ?? undefined,
    queryFn: async ({ pageParam }) => {
    const workspace = await workspaceForEvent(eventId);
    return apiFetch<CampaignList>(
      `/ctf/communications/?workspace_id=${encodeURIComponent(workspace)}&event_id=${encodeURIComponent(eventId)}&limit=25&offset=${pageParam}`,
    );
  } });
}

export function useCancelCtfCommunication(eventId?: string) {
  const client = useQueryClient();
  return useMutation({ mutationFn: (campaignId: string) => apiFetch<Campaign>(
    `/ctf/communications/${campaignId}/cancel/`, { method: "POST", body: {} },
  ), onSuccess: () => client.invalidateQueries({ queryKey: eventId ? keys.event(eventId) : keys.all }) });
}

// --- Multi-event console (#2052) -----------------------------------------------

export function useCommunicationTargets(workspaceId = "") {
  return useInfiniteQuery({
    queryKey: keys.targets(workspaceId),
    initialPageParam: 0,
    getNextPageParam: (page: CommunicationTargets) => page.next_offset ?? undefined,
    queryFn: ({ pageParam, signal }) =>
      apiFetch<CommunicationTargets>("/ctf/communications/targets/", {
        signal,
        query: { workspace_id: workspaceId || undefined, limit: 100, offset: pageParam },
      }),
  });
}

export function useWorkspaceCommunications(workspaceId: string) {
  return useInfiniteQuery({
    queryKey: keys.workspace(workspaceId),
    enabled: Boolean(workspaceId),
    initialPageParam: 0,
    getNextPageParam: (page: CampaignList) => page.next_offset ?? undefined,
    queryFn: ({ pageParam, signal }) =>
      apiFetch<CampaignList>("/ctf/communications/", {
        signal,
        query: { workspace_id: workspaceId, limit: 25, offset: pageParam },
      }),
  });
}

export function useCommunication(campaignId: string) {
  return useQuery({
    queryKey: keys.campaign(campaignId),
    enabled: Boolean(campaignId),
    queryFn: ({ signal }) => apiFetch<Campaign>(`/ctf/communications/${campaignId}/`, { signal }),
  });
}

export function useCommunicationPreview(campaignId: string, revisionId: string) {
  return useQuery({
    queryKey: keys.preview(campaignId, revisionId),
    enabled: Boolean(campaignId && revisionId),
    queryFn: ({ signal }) =>
      apiFetch<CommunicationPreview>(`/ctf/communications/${campaignId}/preview/`, {
        signal,
        query: { revision_id: revisionId },
      }),
  });
}

// Bounded refresh while any delivery command or scheduled release is still pending.
const DELIVERY_REFRESH_MS = 15_000;

function hasPendingWork(pages: DeliveryReport[] | undefined): boolean {
  return (pages ?? []).some((page) =>
    page.results.some(
      (occurrence) =>
        occurrence.status === "scheduled" ||
        occurrence.totals.channels.some((channel) => channel.queued + channel.claimed + channel.retry_due > 0),
    ),
  );
}

export function useCommunicationDelivery(campaignId: string) {
  return useInfiniteQuery({
    queryKey: [...keys.campaign(campaignId), "delivery"],
    enabled: Boolean(campaignId),
    initialPageParam: 0,
    getNextPageParam: (page: DeliveryReport) => page.next_offset ?? undefined,
    refetchInterval: (query) => (hasPendingWork(query.state.data?.pages) ? DELIVERY_REFRESH_MS : false),
    queryFn: ({ pageParam, signal }) =>
      apiFetch<DeliveryReport>(`/ctf/communications/${campaignId}/delivery/`, {
        signal,
        query: { limit: 25, offset: pageParam },
      }),
  });
}

export function useCommunicationRecipients(campaignId: string, intentId: string, eventId: string, offset: number) {
  return useQuery({
    queryKey: [...keys.campaign(campaignId), "recipients", intentId, eventId, offset],
    enabled: Boolean(campaignId && intentId && eventId),
    placeholderData: keepPreviousData,
    queryFn: ({ signal }) =>
      apiFetch<RecipientPage>(`/ctf/communications/${campaignId}/recipients/`, {
        signal,
        query: { intent_id: intentId, event_id: eventId, limit: 50, offset },
      }),
  });
}

export function useCommunicationActivity(campaignId: string) {
  return useQuery({
    queryKey: [...keys.campaign(campaignId), "activity"],
    enabled: Boolean(campaignId),
    queryFn: ({ signal }) => apiFetch<CommunicationActivity>(`/ctf/communications/${campaignId}/activity/`, { signal }),
  });
}

export function useCreateCommunicationDraft() {
  return useMutation({ mutationFn: createDraft });
}

export function useReviseCommunication(campaignId: string) {
  return useMutation({ mutationFn: (content: { subject: string; body: string }) => reviseDraft(campaignId, content) });
}

export function useReleaseCommunication() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ campaignId, revisionId }: { campaignId: string; revisionId: string }) =>
      releaseCommunication(campaignId, revisionId),
    onSuccess: () => client.invalidateQueries({ queryKey: keys.all }),
  });
}
