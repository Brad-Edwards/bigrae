/**
 * Participant communication inbox client (#2053, ADR-051-R13).
 *
 * The authenticated API is the only source of truth. Socket frames, tab focus,
 * reconnect, a bounded poll interval and a same-origin cross-tab signal only
 * start a refetch; none of them carries content or changes a receipt. Keys are
 * qualified by principal and event so another account's or event's private data
 * is never read from the cache.
 */
import { useCallback, useEffect } from "react";
import { useInfiniteQuery, useMutation, useQuery, useQueryClient, type QueryClient } from "@tanstack/react-query";

import { bootstrapKey } from "./bootstrap";
import { apiFetch } from "./client";
import type { components } from "./schema";
import type { Bootstrap } from "./types";

export type InboxItem = components["schemas"]["CommunicationInboxItem"];
export type InboxPage = components["schemas"]["InboxPage"];
export type InboxView = "inbox" | "action_required" | "dismissed";
export type ReceiptAction = "read" | "dismiss" | "acknowledge";

/** The only body profile the participant renderer accepts. */
export const COMMUNICATION_PROFILE = "ctf-communication-markdown/v1";

const PAGE_SIZE = 20;
/** Bound on loaded pages, so a full reconciliation refetch stays finite. */
export const MAX_INBOX_PAGES = 10;
const POLL_MS = 60_000;
const CHANNEL = "shifter-ctf-inbox";

type Principal = number | null;

export const ctfInboxKeys = {
  event: (principal: Principal, eventId: string) => ["ctf", "inbox", principal, eventId] as const,
  list: (principal: Principal, eventId: string, view: InboxView) =>
    [...ctfInboxKeys.event(principal, eventId), "list", view] as const,
  summary: (principal: Principal, eventId: string) => [...ctfInboxKeys.event(principal, eventId), "summary"] as const,
  message: (principal: Principal, eventId: string, messageId: string) =>
    [...ctfInboxKeys.event(principal, eventId), "message", messageId] as const,
};

/** The session principal from the loaded bootstrap; read from cache, never fetched here. */
function usePrincipal(): Principal {
  return useQueryClient().getQueryData<Bootstrap>(bootstrapKey)?.principal.id ?? null;
}

const inboxPath = (eventId: string) => `/ctf/me/events/${encodeURIComponent(eventId)}/communications/`;

const RECONCILE = { refetchOnWindowFocus: true, refetchOnReconnect: true, refetchInterval: POLL_MS } as const;

/** One event's inbox in one view; each refetch reloads every loaded page from offset 0. */
export function useCtfInbox(eventId: string | undefined, view: InboxView) {
  const principal = usePrincipal();
  return useInfiniteQuery({
    queryKey: ctfInboxKeys.list(principal, eventId ?? "", view),
    enabled: Boolean(eventId),
    initialPageParam: 0,
    getNextPageParam: (page: InboxPage, pages: InboxPage[]) =>
      pages.length < MAX_INBOX_PAGES ? (page.next_offset ?? undefined) : undefined,
    queryFn: ({ pageParam, signal }) =>
      apiFetch<InboxPage>(inboxPath(eventId ?? ""), { query: { view, limit: PAGE_SIZE, offset: pageParam }, signal }),
    ...RECONCILE,
  });
}

/** Inbox-wide counts for the workspace shell; reads one row of open obligations. */
export function useCtfInboxSummary(eventId: string | undefined) {
  const principal = usePrincipal();
  return useQuery({
    queryKey: ctfInboxKeys.summary(principal, eventId ?? ""),
    enabled: Boolean(eventId),
    queryFn: ({ signal }) =>
      apiFetch<InboxPage>(inboxPath(eventId ?? ""), { query: { view: "action_required", limit: 1 }, signal }),
    ...RECONCILE,
  });
}

/**
 * One retained message by identity, for an opened message that left the current
 * view. It reauthorizes on every refetch; `placeholder` is the copy already shown.
 */
export function useCtfInboxMessage(eventId: string, messageId: string | undefined, placeholder?: InboxItem) {
  const principal = usePrincipal();
  return useQuery({
    queryKey: ctfInboxKeys.message(principal, eventId, messageId ?? ""),
    enabled: Boolean(messageId),
    queryFn: ({ signal }) =>
      apiFetch<InboxItem>(`${inboxPath(eventId)}${encodeURIComponent(messageId ?? "")}/`, { signal }),
    placeholderData: placeholder,
    ...RECONCILE,
  });
}

/** Refetch every cached inbox view and summary for one event. */
export function invalidateCtfInbox(queryClient: QueryClient, principal: Principal, eventId: string) {
  return queryClient.invalidateQueries({ queryKey: ctfInboxKeys.event(principal, eventId) });
}

/** A stable callback that refetches one event's inbox for the session principal. */
export function useCtfInboxRefetch() {
  const principal = usePrincipal();
  const queryClient = useQueryClient();
  return useCallback((eventId: string) => invalidateCtfInbox(queryClient, principal, eventId), [principal, queryClient]);
}

function announce(principal: Principal, eventId: string) {
  if (typeof BroadcastChannel === "undefined") return;
  const channel = new BroadcastChannel(CHANNEL);
  channel.postMessage({ principal, eventId });
  channel.close();
}

/** Refetch this tab's inbox when another tab of the same origin changes a receipt. */
export function useCtfInboxCrossTabSync(eventId: string | undefined) {
  const principal = usePrincipal();
  const queryClient = useQueryClient();
  useEffect(() => {
    if (!eventId || typeof BroadcastChannel === "undefined") return;
    const channel = new BroadcastChannel(CHANNEL);
    channel.onmessage = (message: MessageEvent<{ principal?: unknown; eventId?: unknown }>) => {
      if (message.data?.principal === principal && message.data?.eventId === eventId) {
        void invalidateCtfInbox(queryClient, principal, eventId);
      }
    };
    return () => channel.close();
  }, [eventId, principal, queryClient]);
}

/**
 * Explicit, idempotent receipt action. It never auto-retries or waits for the
 * network to return: an offline action fails at once and keeps its retry.
 */
export function useCtfReceiptAction(eventId: string) {
  const principal = usePrincipal();
  const queryClient = useQueryClient();
  return useMutation({
    networkMode: "always",
    mutationFn: ({ messageId, action }: { messageId: string; action: ReceiptAction }) =>
      apiFetch<InboxItem>(`${inboxPath(eventId)}${encodeURIComponent(messageId)}/${action}/`, {
        method: "POST",
        body: {},
      }),
    onSettled: async () => {
      // A failed or uncertain response also reconciles from the server.
      await invalidateCtfInbox(queryClient, principal, eventId);
      announce(principal, eventId);
    },
  });
}
