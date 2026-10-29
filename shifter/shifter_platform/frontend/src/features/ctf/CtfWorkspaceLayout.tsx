import { useCallback, useEffect, useRef, useState } from "react";
import { Link, Outlet, useLocation } from "react-router";

import { useQueryClient } from "@tanstack/react-query";

import { ctfKeys, useCtfContentImages, useCtfCurrentEvent } from "@/api/ctf";
import { useCtfInboxCrossTabSync, useCtfInboxRefetch, useCtfInboxSummary } from "@/api/ctfInbox";
import {
  Toast,
  ToastAction,
  ToastDescription,
  ToastProvider,
  ToastTitle,
  ToastViewport,
} from "@/components/ui/toast";

import { ContentImageProvider } from "./contentImages";
import { ctfInboxPath } from "./routes";

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

type Hint = "communication" | "refresh";

interface BusFrame {
  type?: unknown;
  topic?: unknown;
  payload?: { kind?: unknown; snapshot_id?: unknown; intent_id?: unknown };
}

/**
 * Classify one shared-bus frame for this event's topic. The server projects
 * every frame to a reference-only `communication` hint or a generic `refresh`
 * hint (ADR-051-R6); anything else, including a legacy content-bearing frame,
 * is dropped. A hint never carries content and never grants access.
 */
function classifyFrame(raw: string, topic: string): Hint | null {
  let frame: BusFrame;
  try {
    frame = JSON.parse(raw) as BusFrame;
  } catch {
    return null;
  }
  if (frame?.type !== "notification" || frame.topic !== topic || !frame.payload) return null;
  const { kind, snapshot_id: snapshotId, intent_id: intentId } = frame.payload;
  if (kind === "refresh") return "refresh";
  const isRef = (value: unknown) => typeof value === "string" && UUID.test(value);
  return kind === "communication" && isRef(snapshotId) && isRef(intentId) ? "communication" : null;
}

/** Refetch hints for the active event over the shared WS bus (CTF-802, #2053). */
function useEventHints(eventId: string | undefined, onHint: (hint: Hint) => void) {
  const handler = useRef(onHint);
  handler.current = onHint;

  useEffect(() => {
    if (!eventId || typeof WebSocket === "undefined") return;
    const topic = `ctf:event:${eventId}`;
    const protocol = globalThis.location.protocol === "https:" ? "wss" : "ws";
    const socket = new WebSocket(`${protocol}://${globalThis.location.host}/ws/notifications/`);
    socket.onopen = () => {
      socket.send(JSON.stringify({ type: "subscribe", topic }));
    };
    socket.onmessage = (message: MessageEvent<string>) => {
      const hint = classifyFrame(message.data, topic);
      if (hint) handler.current(hint);
    };
    return () => socket.close();
  }, [eventId]);
}

/**
 * Non-modal notice for messages whose pinned policy still needs an action. It
 * is a polite status region: it never takes focus, never blocks navigation and
 * disappears when the obligations are met (WCAG 2.1.2).
 */
function ActionRequiredNotice({ eventId }: Readonly<{ eventId: string | undefined }>) {
  const { pathname } = useLocation();
  const summary = useCtfInboxSummary(eventId);
  const count = summary.isError ? 0 : (summary.data?.action_required_count ?? 0);
  if (count === 0 || pathname.startsWith(ctfInboxPath())) return null;
  return (
    <output className="mx-auto mb-4 flex max-w-5xl flex-wrap items-center gap-3 rounded-md border px-4 py-2 text-sm">
      <span>{count === 1 ? "1 message needs your action." : `${count} messages need your action.`}</span>
      <Link className="underline" to={`${ctfInboxPath()}?view=action_required`}>
        Review messages
      </Link>
    </output>
  );
}

interface LiveToast {
  id: number;
}

/** Participant workspace shell: routes render through the outlet with live hints on top. */
export function CtfWorkspaceLayout() {
  const { data } = useCtfCurrentEvent();
  const eventId = data?.event.id;
  const contentImages = useCtfContentImages();
  const queryClient = useQueryClient();
  const refetchInbox = useCtfInboxRefetch();
  const [toasts, setToasts] = useState<LiveToast[]>([]);
  const nextId = useRef(0);

  useCtfInboxCrossTabSync(eventId);

  const onHint = useCallback(
    (hint: Hint) => {
      if (!eventId) return;
      if (hint === "refresh") {
        void queryClient.invalidateQueries({ queryKey: ctfKeys.all });
        return;
      }
      void refetchInbox(eventId);
      nextId.current += 1;
      setToasts((current) => [...current.slice(-3), { id: nextId.current }]);
    },
    [eventId, queryClient, refetchInbox],
  );

  useEventHints(eventId, onHint);

  const dismiss = useCallback((id: number) => {
    setToasts((current) => current.filter((t) => t.id !== id));
  }, []);

  return (
    <ToastProvider>
      <ActionRequiredNotice eventId={eventId} />
      <ContentImageProvider images={contentImages.data?.images}>
        <Outlet />
      </ContentImageProvider>
      {toasts.map((toast) => (
        <MessageToast key={toast.id} id={toast.id} onDismiss={dismiss} />
      ))}
      <ToastViewport />
    </ToastProvider>
  );
}

/** Content-free toast: the message itself is read only from the authenticated inbox. */
function MessageToast({ id, onDismiss }: Readonly<{ id: number; onDismiss: (id: number) => void }>) {
  return (
    <Toast
      onOpenChange={(open) => {
        if (!open) onDismiss(id);
      }}
    >
      <ToastTitle>New message</ToastTitle>
      <ToastDescription>A new message is in your inbox.</ToastDescription>
      <ToastAction altText="Open inbox" asChild>
        <Link to={ctfInboxPath()}>Open inbox</Link>
      </ToastAction>
    </Toast>
  );
}
