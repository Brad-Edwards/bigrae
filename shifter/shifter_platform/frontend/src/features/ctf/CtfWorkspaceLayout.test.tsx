import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, screen } from "@testing-library/react";

import { renderRoute } from "@/test/utils";

vi.mock("@/api/client", () => ({ apiFetch: vi.fn() }));

import { apiFetch } from "@/api/client";

import { CtfWorkspaceLayout } from "./CtfWorkspaceLayout";

const mockApi = vi.mocked(apiFetch);

class FakeWebSocket {
  static instances: FakeWebSocket[] = [];
  url: string;
  onopen: (() => void) | null = null;
  onmessage: ((event: { data: string }) => void) | null = null;
  sent: string[] = [];

  constructor(url: string) {
    this.url = url;
    FakeWebSocket.instances.push(this);
  }

  send(data: string) {
    this.sent.push(data);
  }

  close() {
    // No-op for tests.
  }
}

const CURRENT_EVENT = {
  event: { id: "e1", name: "Spring CTF", status: "active" },
  participant: { id: "p1", name: "Alice", status: "active" },
};
const INBOX = "/ctf/me/events/e1/communications/";
let actionRequired = 0;

beforeEach(() => {
  actionRequired = 0;
  mockApi.mockReset();
  mockApi.mockImplementation(async (path: string) => {
    if (path === INBOX) {
      return {
        results: [],
        next_offset: null,
        unread_count: actionRequired,
        action_required_count: actionRequired,
        allowed_link_hosts: [],
      };
    }
    if (path.endsWith("/me/event/")) return CURRENT_EVENT;
    return { images: [], announcements: [] };
  });
  FakeWebSocket.instances = [];
  vi.stubGlobal("WebSocket", FakeWebSocket as unknown as typeof WebSocket);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

const inboxCalls = () => mockApi.mock.calls.filter(([path]) => path === INBOX);
const frame = (payload: unknown, topic = "ctf:event:e1") =>
  ({ data: JSON.stringify({ type: "notification", id: 7, event_id: "s1", topic, payload }) });

async function openSocket() {
  renderRoute(<CtfWorkspaceLayout />);
  await vi.waitFor(() => expect(FakeWebSocket.instances).toHaveLength(1));
  await vi.waitFor(() => expect(inboxCalls()).toHaveLength(1));
  const socket = FakeWebSocket.instances[0];
  act(() => socket.onopen?.());
  return socket;
}

describe("CtfWorkspaceLayout", () => {
  it("treats a communication wake-up as a refetch hint, never as a read", async () => {
    const socket = await openSocket();
    expect(socket.url).toContain("/ws/notifications/");
    expect(socket.sent[0]).toContain("ctf:event:e1");

    act(() =>
      socket.onmessage?.(
        frame({
          kind: "communication",
          snapshot_id: "3f2c3f7e-1d2b-4a5c-9e8f-0a1b2c3d4e5f",
          intent_id: "9a8b7c6d-5e4f-4a3b-8c2d-1e0f9a8b7c6d",
        }),
      ),
    );

    expect(await screen.findByText("New message")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Open inbox" })).toHaveAttribute("href", "/ctf/inbox/");
    await vi.waitFor(() => expect(inboxCalls()).toHaveLength(2));
    // The socket frame carries references only; nothing is posted on its behalf.
    expect(mockApi.mock.calls.some(([, options]) => options?.method === "POST")).toBe(false);
  });

  it("refetches on refresh hints and drops malformed or foreign frames silently", async () => {
    const socket = await openSocket();

    act(() => socket.onmessage?.(frame({ kind: "refresh" })));
    await vi.waitFor(() => expect(inboxCalls()).toHaveLength(2));
    expect(screen.queryByText("New message")).not.toBeInTheDocument();

    act(() => socket.onmessage?.({ data: "not-json" }));
    act(() => socket.onmessage?.(frame({ kind: "communication", snapshot_id: "x" })));
    act(() => socket.onmessage?.(frame({ kind: "refresh" }, "ctf:event:other")));
    // A legacy content-bearing frame is never rendered as content.
    act(() => socket.onmessage?.(frame({ kind: "first_blood", participant_name: "<b>Bob</b>" })));
    expect(screen.queryByText("New message")).not.toBeInTheDocument();
    expect(screen.queryByText(/Bob/)).not.toBeInTheDocument();
    expect(inboxCalls()).toHaveLength(2);
  });

  it("announces open obligations without moving focus or blocking navigation", async () => {
    actionRequired = 2;
    renderRoute(<CtfWorkspaceLayout />);

    const notice = await screen.findByRole("status");
    expect(notice).toHaveTextContent("2 messages need your action.");
    expect(screen.getByRole("link", { name: "Review messages" })).toHaveAttribute(
      "href",
      "/ctf/inbox/?view=action_required",
    );
    expect(document.activeElement).toBe(document.body);
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
  });
});
