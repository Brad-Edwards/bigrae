/**
 * Organizer alert inbox client (#528, ADR-065-R4).
 *
 * The event owner and live co-organizers read system alerts for one event:
 * automatic start and end, a refused start, and ranges not ready at start. The
 * API rechecks policy on every request. Keys are qualified by principal and
 * event so another account's or event's alerts are never read from the cache.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { bootstrapKey } from "./bootstrap";
import { apiFetch } from "./client";
import type { InboxItem, InboxPage } from "./ctfInbox";
import type { Bootstrap } from "./types";

export type OrganizerAlertView = "inbox" | "dismissed";
export type OrganizerAlertAction = "read" | "dismiss";

const PAGE_SIZE = 50;
const POLL_MS = 60_000;

type Principal = number | null;

export const ctfOrganizerAlertKeys = {
  event: (principal: Principal, eventId: string) => ["ctf", "organizer-alerts", principal, eventId] as const,
  list: (principal: Principal, eventId: string, view: OrganizerAlertView) =>
    [...ctfOrganizerAlertKeys.event(principal, eventId), view] as const,
};

function usePrincipal(): Principal {
  return useQueryClient().getQueryData<Bootstrap>(bootstrapKey)?.principal.id ?? null;
}

const alertsPath = (eventId: string) => `/ctf/events/${encodeURIComponent(eventId)}/organizer-alerts/`;

/** The newest alerts of one view for one event; refetched on focus, reconnect and a bounded poll. */
export function useCtfOrganizerAlerts(eventId: string, view: OrganizerAlertView) {
  const principal = usePrincipal();
  return useQuery({
    queryKey: ctfOrganizerAlertKeys.list(principal, eventId, view),
    enabled: Boolean(eventId),
    queryFn: ({ signal }) =>
      apiFetch<InboxPage>(alertsPath(eventId), { query: { view, limit: PAGE_SIZE, offset: 0 }, signal }),
    refetchOnWindowFocus: true,
    refetchOnReconnect: true,
    refetchInterval: POLL_MS,
  });
}

/** Explicit, idempotent read or dismiss; every outcome reconciles from the server. */
export function useCtfOrganizerAlertAction(eventId: string) {
  const principal = usePrincipal();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ messageId, action }: { messageId: string; action: OrganizerAlertAction }) =>
      apiFetch<InboxItem>(`${alertsPath(eventId)}${encodeURIComponent(messageId)}/${action}/`, {
        method: "POST",
        body: {},
      }),
    onSettled: () => queryClient.invalidateQueries({ queryKey: ctfOrganizerAlertKeys.event(principal, eventId) }),
  });
}
