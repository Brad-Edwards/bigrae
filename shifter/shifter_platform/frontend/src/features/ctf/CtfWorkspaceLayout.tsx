import { useCallback, useEffect, useRef, useState } from "react";
import { Outlet } from "react-router";

import { useQueryClient } from "@tanstack/react-query";

import { ctfKeys, useCtfContentImages, useCtfCurrentEvent } from "@/api/ctf";
import {
  Toast,
  ToastDescription,
  ToastProvider,
  ToastTitle,
  ToastViewport,
} from "@/components/ui/toast";

import { ContentImageProvider } from "./contentImages";

interface LiveNotification {
  id: number;
  kind: string;
  title: string;
  description: string;
}

interface BusMessage {
  type?: string;
  payload?: { kind?: string; subject?: string; challenge_name?: string; participant_name?: string };
}

function describeNotification(payload: NonNullable<BusMessage["payload"]>): Omit<LiveNotification, "id"> | null {
  switch (payload.kind) {
    case "announcement":
      return { kind: "announcement", title: "Announcement", description: payload.subject ?? "" };
    case "challenge_released":
      return {
        kind: "challenge_released",
        title: "New challenge",
        description: payload.challenge_name ?? "A new challenge just dropped.",
      };
    case "first_blood":
      return {
        kind: "first_blood",
        title: "First blood!",
        description: `${payload.participant_name ?? "Someone"} drew first blood on ${payload.challenge_name ?? "a challenge"}.`,
      };
    case "range_ready":
      return { kind: "range_ready", title: "Range ready", description: "Your range is provisioned and ready." };
    default:
      return null;
  }
}

/** Live event notifications over the shared WS bus (CTF-802), shown as toasts. */
function useEventNotifications(eventId: string | undefined, onNotification: (n: Omit<LiveNotification, "id">) => void) {
  const handler = useRef(onNotification);
  handler.current = onNotification;

  useEffect(() => {
    if (!eventId || typeof WebSocket === "undefined") return;
    const protocol = globalThis.location.protocol === "https:" ? "wss" : "ws";
    const socket = new WebSocket(`${protocol}://${globalThis.location.host}/ws/notifications/`);
    socket.onopen = () => {
      socket.send(JSON.stringify({ type: "subscribe", topic: `ctf:event:${eventId}` }));
    };
    socket.onmessage = (message: MessageEvent<string>) => {
      let parsed: BusMessage | null;
      try {
        parsed = JSON.parse(message.data) as BusMessage | null;
      } catch {
        return; // Malformed frames are dropped; the socket stays up.
      }
      if (parsed?.type !== "notification" || !parsed.payload) return;
      const described = describeNotification(parsed.payload);
      if (described) handler.current(described);
    };
    return () => socket.close();
  }, [eventId]);
}

/** Participant workspace shell: routes render through the outlet with live toasts on top. */
export function CtfWorkspaceLayout() {
  const { data } = useCtfCurrentEvent();
  const contentImages = useCtfContentImages();
  const queryClient = useQueryClient();
  const [toasts, setToasts] = useState<LiveNotification[]>([]);
  const nextId = useRef(0);

  const push = useCallback(
    (notification: Omit<LiveNotification, "id">) => {
      nextId.current += 1;
      setToasts((current) => [...current.slice(-3), { ...notification, id: nextId.current }]);
      if (notification.kind === "announcement") {
        queryClient.invalidateQueries({ queryKey: ctfKeys.announcements() });
        queryClient.invalidateQueries({ queryKey: ctfKeys.contentImages() });
      }
      if (notification.kind === "challenge_released") {
        queryClient.invalidateQueries({ queryKey: ctfKeys.challenges() });
        queryClient.invalidateQueries({ queryKey: ctfKeys.contentImages() });
      }
      if (notification.kind === "range_ready") {
        queryClient.invalidateQueries({ queryKey: ctfKeys.rangeStatus() });
      }
    },
    [queryClient],
  );

  useEventNotifications(data?.event.id, push);

  const dismiss = useCallback((id: number) => {
    setToasts((current) => current.filter((t) => t.id !== id));
  }, []);

  return (
    <ToastProvider>
      <ContentImageProvider images={contentImages.data?.images}>
        <Outlet />
      </ContentImageProvider>
      {toasts.map((toast) => (
        <LiveToast key={toast.id} toast={toast} onDismiss={dismiss} />
      ))}
      <ToastViewport />
    </ToastProvider>
  );
}

function LiveToast({
  toast,
  onDismiss,
}: Readonly<{ toast: LiveNotification; onDismiss: (id: number) => void }>) {
  return (
    <Toast
      onOpenChange={(open) => {
        if (!open) onDismiss(toast.id);
      }}
    >
      <ToastTitle>{toast.title}</ToastTitle>
      <ToastDescription>{toast.description}</ToastDescription>
    </Toast>
  );
}
