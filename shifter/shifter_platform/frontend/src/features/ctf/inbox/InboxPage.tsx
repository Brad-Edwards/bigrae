/**
 * Participant inbox for the active event (#2053, ADR-051-R13).
 *
 * Messages come only from the authenticated, event-scoped inbox API. Listing,
 * paging and socket hints never change a receipt; opening a supported message
 * records a read, and dismiss and acknowledge are separate explicit actions. An
 * unknown content profile fails closed, and an authorization loss drops every
 * loaded message instead of showing stale private content.
 */
import { useCallback, useEffect, useId, useMemo, useRef, useState, useSyncExternalStore } from "react";
import { Link, useSearchParams } from "react-router";

import { onlineManager, useQueryClient } from "@tanstack/react-query";

import { useCtfCurrentEvent } from "@/api/ctf";
import {
  COMMUNICATION_PROFILE,
  MAX_INBOX_PAGES,
  useCtfInbox,
  useCtfInboxMessage,
  useCtfReceiptAction,
  type InboxItem,
  type InboxView,
  type ReceiptAction,
} from "@/api/ctfInbox";
import { ApiError } from "@/api/errors";
import { PageHeader } from "@/components/page-header";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

import { formatDateTime } from "../format";
import { MarkdownContent } from "../MarkdownContent";
import { ctfInboxPath } from "../routes";

const VIEWS: readonly { view: InboxView; label: string; empty: string }[] = [
  { view: "inbox", label: "Inbox", empty: "No messages." },
  { view: "action_required", label: "Needs action", empty: "No messages need your action." },
  { view: "dismissed", label: "Dismissed", empty: "No dismissed messages." },
];

function useOnline(): boolean {
  return useSyncExternalStore(
    (onChange) => onlineManager.subscribe(onChange),
    () => onlineManager.isOnline(),
  );
}

function isUnavailable(error: unknown): boolean {
  return error instanceof ApiError && (error.status === 403 || error.status === 404);
}

function obligationLabel(item: InboxItem): string | null {
  if (!item.action_required) return null;
  return item.acknowledgement_policy === "explicit" ? "Needs acknowledgement" : "Needs reading";
}

export function InboxPage() {
  const [params] = useSearchParams();
  const requested = params.get("view");
  const current = VIEWS.find((v) => v.view === requested) ?? VIEWS[0];
  const event = useCtfCurrentEvent();
  const eventId = event.data?.event.id;

  return (
    <div className="mx-auto max-w-5xl p-6">
      <PageHeader title="Inbox" description="Messages from the event organizers" />
      <nav aria-label="Inbox views" className="mb-6 flex flex-wrap gap-2">
        {VIEWS.map(({ view, label }) => (
          <Link
            key={view}
            to={view === "inbox" ? ctfInboxPath() : `${ctfInboxPath()}?view=${view}`}
            aria-current={view === current.view ? "page" : undefined}
            className={cn(
              "rounded-md border px-3 py-1 text-sm",
              view === current.view ? "bg-secondary font-medium" : "hover:bg-secondary/60",
            )}
          >
            {label}
          </Link>
        ))}
      </nav>
      <InboxBody eventError={event.isError} eventId={eventId} view={current} />
    </div>
  );
}

function InboxBody({
  eventError,
  eventId,
  view,
}: Readonly<{ eventError: boolean; eventId: string | undefined; view: (typeof VIEWS)[number] }>) {
  if (eventError) return <p className="text-sm text-muted-foreground">You are not taking part in an active event.</p>;
  if (!eventId) return <p className="text-sm text-muted-foreground">Loading messages…</p>;
  return <InboxList key={`${eventId}:${view.view}`} eventId={eventId} view={view.view} empty={view.empty} />;
}

function InboxList({ eventId, view, empty }: Readonly<{ eventId: string; view: InboxView; empty: string }>) {
  const query = useCtfInbox(eventId, view);
  const queryClient = useQueryClient();
  const online = useOnline();
  // The opened message, its list position, and the copy shown when it opened.
  const [opened, setOpened] = useState<{ id: string; index: number; item: InboxItem } | null>(null);
  // One read request in flight or done per message per page visit; a failure frees it again.
  const readRequested = useRef(new Set<string>());
  const unavailable = isUnavailable(query.error);
  const hasData = Boolean(query.data);

  useEffect(() => {
    // Authorization loss: drop the cached private pages, not just hide them.
    if (unavailable && hasData) void queryClient.resetQueries({ queryKey: ["ctf", "inbox"] });
  }, [unavailable, hasData, queryClient]);

  const listed = useMemo(() => {
    const seen = new Set<string>();
    return (query.data?.pages ?? [])
      .flatMap((p) => p.results)
      .filter((item) => !seen.has(item.message_id) && Boolean(seen.add(item.message_id)));
  }, [query.data]);
  // An action can move the open message out of this view (a read clears "Needs action").
  // Keep it in place, refreshed through the authorized detail API, until it is closed.
  const left = opened !== null && !listed.some((item) => item.message_id === opened.id);
  const detail = useCtfInboxMessage(eventId, left ? opened.id : undefined, opened?.item);
  const detailGone = isUnavailable(detail.error);
  useEffect(() => {
    if (detailGone) setOpened(null);
  }, [detailGone]);
  const items = useMemo(() => {
    if (!left || !detail.data || detailGone) return listed;
    const merged = [...listed];
    merged.splice(Math.min(opened.index, merged.length), 0, detail.data);
    return merged;
  }, [listed, left, detail.data, detailGone, opened]);
  const pages = query.data?.pages ?? [];
  const hosts = pages[0]?.allowed_link_hosts ?? [];
  const truncated = pages.length >= MAX_INBOX_PAGES && pages.at(-1)?.next_offset != null;

  if (unavailable) {
    return (
      <Alert>
        <AlertTitle>Your inbox is not available.</AlertTitle>
        <AlertDescription>You can only read messages for an event you are taking part in.</AlertDescription>
      </Alert>
    );
  }
  if (query.isError) {
    return (
      <Alert variant="destructive">
        <AlertTitle>Could not load messages.</AlertTitle>
        <AlertDescription>
          <Button variant="outline" size="sm" onClick={() => void query.refetch()}>
            Try again
          </Button>
        </AlertDescription>
      </Alert>
    );
  }
  if (query.isPending) return <p className="text-sm text-muted-foreground">Loading messages…</p>;

  return (
    <div className="flex flex-col gap-4">
      {online ? null : <p className="text-sm text-muted-foreground">You are offline. Messages can be out of date.</p>}
      {items.length === 0 ? (
        <p className="text-sm text-muted-foreground">{empty}</p>
      ) : (
        <ul className="flex flex-col gap-2">
          {items.map((item, index) => (
            <InboxEntry
              key={item.message_id}
              item={item}
              eventId={eventId}
              hosts={hosts}
              online={online}
              readRequested={readRequested.current}
              open={opened?.id === item.message_id}
              onToggle={() =>
                setOpened((current) =>
                  current?.id === item.message_id ? null : { id: item.message_id, index, item },
                )
              }
            />
          ))}
        </ul>
      )}
      {query.hasNextPage ? (
        <Button
          variant="outline"
          className="self-start"
          disabled={query.isFetchingNextPage}
          onClick={() => void query.fetchNextPage()}
        >
          Load older messages
        </Button>
      ) : null}
      {truncated ? <p className="text-xs text-muted-foreground">Only the most recent messages are shown.</p> : null}
    </div>
  );
}

function InboxEntry({
  item,
  eventId,
  hosts,
  online,
  readRequested,
  open,
  onToggle,
}: Readonly<{
  item: InboxItem;
  eventId: string;
  hosts: readonly string[];
  online: boolean;
  readRequested: Set<string>;
  open: boolean;
  onToggle: () => void;
}>) {
  const subjectId = useId();
  const panelId = useId();
  const obligation = obligationLabel(item);
  return (
    <li className="rounded-md border">
      <h2 className="text-sm">
        <button
          type="button"
          aria-expanded={open}
          aria-controls={panelId}
          onClick={onToggle}
          className="flex w-full flex-wrap items-center gap-2 px-4 py-3 text-left"
        >
          <span id={subjectId} className={cn("min-w-0 flex-1", item.read_at ? "" : "font-semibold")}>
            {item.subject}
          </span>
          {item.read_at ? null : <Badge variant="secondary">Unread</Badge>}
          {obligation ? <Badge variant="destructive">{obligation}</Badge> : null}
          {item.dismissed_at ? <Badge variant="outline">Dismissed</Badge> : null}
          <span className="text-xs text-muted-foreground">{formatDateTime(item.created_at)}</span>
        </button>
      </h2>
      {open ? (
        <section id={panelId} aria-labelledby={subjectId} className="border-t px-4 py-3">
          <MessageBody item={item} eventId={eventId} hosts={hosts} online={online} readRequested={readRequested} />
        </section>
      ) : null}
    </li>
  );
}

function MessageBody({
  item,
  eventId,
  hosts,
  online,
  readRequested,
}: Readonly<{
  item: InboxItem;
  eventId: string;
  hosts: readonly string[];
  online: boolean;
  readRequested: Set<string>;
}>) {
  const receipt = useCtfReceiptAction(eventId);
  const supported = item.content_profile === COMMUNICATION_PROFILE;
  const { mutateAsync } = receipt;

  const read = useCallback(() => {
    readRequested.add(item.message_id);
    // Not a component callback: the guard is released even if this body closes first.
    mutateAsync({ messageId: item.message_id, action: "read" }).catch(() => readRequested.delete(item.message_id));
  }, [item.message_id, mutateAsync, readRequested]);

  useEffect(() => {
    // Opening a supported, rendered message is the read; a list, page or hint is not.
    if (!supported || item.read_at || readRequested.has(item.message_id)) return;
    read();
  }, [supported, item.read_at, item.message_id, read, readRequested]);

  const act = (action: ReceiptAction) => receipt.mutate({ messageId: item.message_id, action });
  const busy = receipt.isPending || !online;
  const failedAction = receipt.isError ? receipt.variables?.action : undefined;

  if (!supported) {
    return <p className="text-sm text-muted-foreground">This message uses a format this page cannot show.</p>;
  }
  return (
    <div className="flex flex-col gap-3">
      <MarkdownContent text={item.body} profile={COMMUNICATION_PROFILE} allowedLinkHosts={hosts} />
      <div className="flex flex-wrap items-center gap-2">
        {item.acknowledgement_policy === "explicit" && !item.acknowledged_at ? (
          <Button size="sm" disabled={busy} onClick={() => act("acknowledge")}>
            Acknowledge
          </Button>
        ) : null}
        {item.acknowledged_at ? (
          <span className="text-xs text-muted-foreground">Acknowledged {formatDateTime(item.acknowledged_at)}</span>
        ) : null}
        {!item.dismissed_at && !item.action_required ? (
          <Button size="sm" variant="outline" disabled={busy} onClick={() => act("dismiss")}>
            Dismiss
          </Button>
        ) : null}
      </div>
      {failedAction === "read" && !item.read_at ? (
        <div role="alert" className="flex flex-wrap items-center gap-2 text-sm text-destructive">
          <span>Could not record that you read this message.</span>
          <Button size="sm" variant="outline" disabled={busy} onClick={read}>
            Try again
          </Button>
        </div>
      ) : null}
      {failedAction && failedAction !== "read" ? (
        <p role="alert" className="text-sm text-destructive">
          The action did not complete. Try again.
        </p>
      ) : null}
    </div>
  );
}
