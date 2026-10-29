import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, screen, within } from "@testing-library/react";
import { onlineManager } from "@tanstack/react-query";

import { ApiError } from "@/api/errors";
import type { InboxItem, InboxPage as Page } from "@/api/ctfInbox";
import { renderRoute, setupUser } from "@/test/utils";

vi.mock("@/api/client", () => ({ apiFetch: vi.fn() }));

import { apiFetch } from "@/api/client";

import { InboxPage } from "./InboxPage";

const mockApi = vi.mocked(apiFetch);
const INBOX = "/ctf/me/events/e1/communications/";

function item(id: string, overrides: Partial<InboxItem> = {}): InboxItem {
  return {
    message_id: id,
    subject: `Subject ${id}`,
    body: `Body ${id} with [docs](https://docs.example.test/rules) and [bad](https://untrusted.test/x)`,
    content_profile: "ctf-communication-markdown/v1",
    origin: "organizer_staff",
    acknowledgement_policy: "none",
    read_at: null,
    acknowledged_at: null,
    dismissed_at: null,
    action_required: false,
    created_at: "2026-10-29T10:00:00Z",
    ...overrides,
  };
}

const page = (results: InboxItem[], extra: Partial<Page> = {}): Page => ({
  results,
  next_offset: null,
  unread_count: results.filter((r) => !r.read_at).length,
  action_required_count: results.filter((r) => r.action_required).length,
  allowed_link_hosts: ["docs.example.test"],
  ...extra,
});

type Handler = (query: Record<string, unknown>) => Page | Promise<Page>;
let listHandler: Handler;
let postHandler: (action: string) => InboxItem;
let detailHandler: (id: string) => InboxItem;
const posts: string[] = [];

beforeEach(() => {
  posts.length = 0;
  listHandler = () => page([item("m1")]);
  postHandler = () => item("m1");
  detailHandler = (id) => item(id);
  mockApi.mockReset();
  mockApi.mockImplementation(async (path: string, options = {}) => {
    if (path.endsWith("/me/event/")) return { event: { id: "e1", name: "Spring CTF" }, participant: { id: "p1" } };
    if (path === INBOX) return listHandler(options.query ?? {});
    if (options.method === "POST") {
      const action = path.slice(INBOX.length);
      posts.push(action);
      return postHandler(action);
    }
    if (path.startsWith(INBOX)) return detailHandler(path.slice(INBOX.length, -1));
    throw new Error(`unexpected ${path}`);
  });
});

afterEach(() => {
  onlineManager.setOnline(true);
});

const renderInbox = (search = "") =>
  renderRoute(<InboxPage />, { path: "/ctf/inbox/", initialEntries: [`/ctf/inbox/${search}`] });

describe("InboxPage", () => {
  it("lists messages without changing receipts and reads only on an explicit open", async () => {
    const user = setupUser();
    renderInbox();

    const toggle = await screen.findByRole("button", { name: /Subject m1/ });
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(screen.getByText("Unread")).toBeInTheDocument();
    expect(screen.queryByText(/Body m1/)).not.toBeInTheDocument();
    expect(posts).toEqual([]);

    await user.click(toggle);
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    const region = screen.getByRole("region", { name: "Subject m1" });
    expect(within(region).getByRole("link", { name: "docs" })).toHaveAttribute(
      "href",
      "https://docs.example.test/rules",
    );
    expect(within(region).queryByRole("link", { name: "bad" })).not.toBeInTheDocument();
    await vi.waitFor(() => expect(posts).toEqual(["m1/read/"]));

    await user.click(toggle);
    await user.click(toggle);
    expect(posts).toEqual(["m1/read/"]);
  });

  it("acknowledges required messages and offers dismiss only without an open obligation", async () => {
    const user = setupUser();
    listHandler = () =>
      page([
        item("m1", { acknowledgement_policy: "explicit", action_required: true, read_at: "2026-10-29T10:01:00Z" }),
        item("m2", { read_at: "2026-10-29T10:01:00Z" }),
      ]);
    renderInbox();

    await user.click(await screen.findByRole("button", { name: /Subject m1/ }));
    const required = screen.getByRole("region", { name: "Subject m1" });
    expect(screen.getAllByText("Needs acknowledgement").length).toBeGreaterThan(0);
    expect(within(required).queryByRole("button", { name: "Dismiss" })).not.toBeInTheDocument();
    await user.click(within(required).getByRole("button", { name: "Acknowledge" }));
    await vi.waitFor(() => expect(posts).toContain("m1/acknowledge/"));

    await user.click(screen.getByRole("button", { name: /Subject m2/ }));
    const plain = screen.getByRole("region", { name: "Subject m2" });
    expect(within(plain).queryByRole("button", { name: "Acknowledge" })).not.toBeInTheDocument();
    await user.click(within(plain).getByRole("button", { name: "Dismiss" }));
    await vi.waitFor(() => expect(posts).toContain("m2/dismiss/"));
    expect(posts).not.toContain("m1/read/");
    expect(posts).not.toContain("m2/read/");
  });

  it("keeps a failed read retryable, explicitly and on reopen", async () => {
    const user = setupUser();
    let failRead = true;
    postHandler = (action) => {
      if (action === "m1/read/" && failRead) throw new ApiError(503, { code: "unavailable", message: "Unavailable" });
      return item("m1", { read_at: "2026-10-29T10:01:00Z" });
    };
    renderInbox();
    const toggle = await screen.findByRole("button", { name: /Subject m1/ });
    const failure = "Could not record that you read this message.";

    await user.click(toggle);
    expect(await screen.findByText(failure)).toBeInTheDocument();

    // A failure frees the message, so the next open retries the read.
    await user.click(toggle);
    await user.click(toggle);
    await vi.waitFor(() => expect(posts).toEqual(["m1/read/", "m1/read/"]));
    expect(await screen.findByText(failure)).toBeInTheDocument();

    failRead = false;
    await user.click(screen.getByRole("button", { name: "Try again" }));
    await vi.waitFor(() => expect(posts).toEqual(["m1/read/", "m1/read/", "m1/read/"]));
    await vi.waitFor(() => expect(screen.queryByText(failure)).not.toBeInTheDocument());
  });

  it("keeps an opened message in place when its read removes it from the view", async () => {
    const user = setupUser();
    const required = item("m2", { acknowledgement_policy: "read", action_required: true });
    let read = false;
    listHandler = () => page(read ? [item("m1")] : [item("m1"), required, item("m3")]);
    postHandler = () => {
      read = true;
      return { ...required, read_at: "2026-10-29T10:01:00Z", action_required: false };
    };
    detailHandler = (id) => ({ ...required, message_id: id, read_at: read ? "2026-10-29T10:01:00Z" : null, action_required: !read });
    renderInbox("?view=action_required");

    await user.click(await screen.findByRole("button", { name: /Subject m2/ }));
    await vi.waitFor(() => expect(posts).toEqual(["m2/read/"]));
    await vi.waitFor(() => expect(mockApi.mock.calls.some(([path]) => path === `${INBOX}m2/`)).toBe(true));
    const region = screen.getByRole("region", { name: "Subject m2" });
    expect(within(region).getByText(/Body m2/)).toBeInTheDocument();
    const order = screen.getAllByRole("button", { name: /Subject m/ }).map((b) => b.textContent?.slice(0, 10));
    expect(order).toEqual(["Subject m1", "Subject m2"]);

    await user.click(screen.getByRole("button", { name: /Subject m2/ }));
    expect(screen.queryByRole("button", { name: /Subject m2/ })).not.toBeInTheDocument();
  });

  it("fails closed for an unknown content profile", async () => {
    const user = setupUser();
    listHandler = () =>
      page([item("m1", { content_profile: "html/v9", acknowledgement_policy: "explicit", action_required: true })]);
    renderInbox();

    await user.click(await screen.findByRole("button", { name: /Subject m1/ }));
    const region = screen.getByRole("region", { name: "Subject m1" });
    expect(within(region).getByText("This message uses a format this page cannot show.")).toBeInTheDocument();
    expect(within(region).queryByText(/Body m1/)).not.toBeInTheDocument();
    expect(within(region).queryByRole("button", { name: "Acknowledge" })).not.toBeInTheDocument();
    expect(posts).toEqual([]);
  });

  it("drops loaded messages when authorization is lost", async () => {
    const user = setupUser();
    listHandler = () => page([item("m1", { read_at: "2026-10-29T10:01:00Z" })]);
    renderInbox();
    await user.click(await screen.findByRole("button", { name: /Subject m1/ }));

    listHandler = () => {
      throw new ApiError(404, { code: "not_found", message: "Resource not found" });
    };
    await user.click(screen.getByRole("button", { name: "Dismiss" }));

    expect(await screen.findByText("Your inbox is not available.")).toBeInTheDocument();
    expect(screen.queryByText("Subject m1")).not.toBeInTheDocument();
  });

  it("uses only the closed view set and never renders the query value", async () => {
    const seen: unknown[] = [];
    listHandler = (query) => {
      seen.push(query.view);
      return page([]);
    };
    renderInbox("?view=%3Cscript%3Ealert(1)%3C%2Fscript%3E");

    expect(await screen.findByText("No messages.")).toBeInTheDocument();
    expect(seen).toEqual(["inbox"]);
    expect(document.body.innerHTML).not.toContain("script");
    expect(screen.getByRole("link", { name: "Inbox" })).toHaveAttribute("aria-current", "page");
  });

  it("loads further pages once and merges them by message identity", async () => {
    const user = setupUser();
    listHandler = (query) =>
      query.offset === 0
        ? page([item("m1"), item("m2")], { next_offset: 20 })
        : page([item("m2"), item("m3")]);
    renderInbox("?view=dismissed");

    await user.click(await screen.findByRole("button", { name: "Load older messages" }));
    expect(await screen.findByRole("button", { name: /Subject m3/ })).toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: /Subject m2/ })).toHaveLength(1);
    expect(screen.queryByRole("button", { name: "Load older messages" })).not.toBeInTheDocument();
  });

  it("marks offline data as possibly old and blocks receipt actions", async () => {
    const user = setupUser();
    listHandler = () => page([item("m1", { read_at: "2026-10-29T10:01:00Z" })]);
    renderInbox();
    await user.click(await screen.findByRole("button", { name: /Subject m1/ }));

    act(() => onlineManager.setOnline(false));
    expect(screen.getByText("You are offline. Messages can be out of date.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Dismiss" })).toBeDisabled();
  });
});
