import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ReactNode } from "react";
import { act, renderHook } from "@testing-library/react";
import { QueryClient, QueryClientProvider, onlineManager } from "@tanstack/react-query";

vi.mock("./client", () => ({ apiFetch: vi.fn() }));

import { apiFetch } from "./client";
import { bootstrapKey } from "./bootstrap";
import { ctfInboxKeys, useCtfInbox, useCtfInboxCrossTabSync, useCtfReceiptAction } from "./ctfInbox";

const mockApi = vi.mocked(apiFetch);

class FakeChannel {
  static open: FakeChannel[] = [];
  static posted: unknown[] = [];
  onmessage: ((event: MessageEvent) => void) | null = null;

  constructor(readonly name: string) {
    FakeChannel.open.push(this);
  }

  postMessage(data: unknown) {
    FakeChannel.posted.push(data);
    for (const other of FakeChannel.open) {
      if (other !== this && other.name === this.name) other.onmessage?.({ data } as MessageEvent);
    }
  }

  close() {
    FakeChannel.open = FakeChannel.open.filter((c) => c !== this);
  }
}

function setup(principal = 7) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  client.setQueryData(bootstrapKey, { principal: { id: principal } });
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
  return { client, wrapper };
}

const listCalls = (eventId: string) =>
  mockApi.mock.calls.filter(([path, options]) => path === `/ctf/me/events/${eventId}/communications/` && !options?.method);

beforeEach(() => {
  FakeChannel.open = [];
  FakeChannel.posted = [];
  vi.stubGlobal("BroadcastChannel", FakeChannel);
  mockApi.mockReset();
  mockApi.mockImplementation(async (_path: string, options = {}) =>
    options.method === "POST"
      ? { message_id: "m1" }
      : { results: [], next_offset: null, unread_count: 0, action_required_count: 0, allowed_link_hosts: [] },
  );
});

afterEach(() => {
  vi.unstubAllGlobals();
  onlineManager.setOnline(true);
});

describe("participant inbox client", () => {
  it("qualifies cached inbox data by principal, event and view", async () => {
    const { client, wrapper } = setup(7);
    renderHook(() => useCtfInbox("e1", "inbox"), { wrapper });
    renderHook(() => useCtfInbox("e2", "inbox"), { wrapper });
    await vi.waitFor(() => expect(client.getQueryData(ctfInboxKeys.list(7, "e1", "inbox"))).toBeDefined());
    await vi.waitFor(() => expect(client.getQueryData(ctfInboxKeys.list(7, "e2", "inbox"))).toBeDefined());
    expect(client.getQueryData(ctfInboxKeys.list(8, "e1", "inbox"))).toBeUndefined();
    expect(listCalls("e1")[0][1]?.query).toEqual({ view: "inbox", limit: 20, offset: 0 });
  });

  it("refetches when another tab changes a receipt for the same principal and event only", async () => {
    const { wrapper } = setup(7);
    const { result } = renderHook(
      () => {
        useCtfInboxCrossTabSync("e1");
        return useCtfInbox("e1", "inbox");
      },
      { wrapper },
    );
    await vi.waitFor(() => expect(result.current.isSuccess).toBe(true));

    const otherTab = new FakeChannel("shifter-ctf-inbox");
    act(() => otherTab.postMessage({ principal: 7, eventId: "e2" }));
    act(() => otherTab.postMessage({ principal: 8, eventId: "e1" }));
    expect(listCalls("e1")).toHaveLength(1);
    act(() => otherTab.postMessage({ principal: 7, eventId: "e1" }));
    await vi.waitFor(() => expect(listCalls("e1")).toHaveLength(2));
  });

  it("sends a receipt action at once while offline and then announces it to other tabs", async () => {
    const { wrapper } = setup(7);
    const { result } = renderHook(() => useCtfReceiptAction("e1"), { wrapper });
    act(() => onlineManager.setOnline(false));

    act(() => result.current.mutate({ messageId: "m1", action: "dismiss" }));
    await vi.waitFor(() =>
      expect(mockApi).toHaveBeenCalledWith("/ctf/me/events/e1/communications/m1/dismiss/", {
        method: "POST",
        body: {},
      }),
    );
    await vi.waitFor(() => expect(FakeChannel.posted).toEqual([{ principal: 7, eventId: "e1" }]));
  });
});
