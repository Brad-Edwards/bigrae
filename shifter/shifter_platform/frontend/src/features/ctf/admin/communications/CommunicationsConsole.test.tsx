import { afterEach, expect, it, vi } from "vitest";
import { screen, waitFor, within } from "@testing-library/react";
import { renderRoute, setupUser } from "@/test/utils";

import { CommunicationDetailPage } from "./CommunicationDetailPage";
import { CommunicationsPage } from "./CommunicationsPage";

afterEach(() => vi.unstubAllGlobals());

type Reply = [number, unknown];

const TARGETS = {
  workspaces: [{ id: "w1", name: "Ops" }],
  events: [
    { id: "e1", name: "Alpha", status: "registration", event_timezone: "UTC", event_start: "", event_end: "", workspace_id: "w1" },
    { id: "e2", name: "Bravo", status: "active", event_timezone: "America/New_York", event_start: "", event_end: "", workspace_id: "w1" },
  ],
  next_offset: null,
};
const CAMPAIGN = {
  id: "c1", title: "Kickoff", status: "draft", origin: "organizer_staff", channels: ["in_app"],
  acknowledgement_policy: "none", target_event_count: 2, created_at: "2026-10-01T00:00:00Z",
  target_events: [{ id: "e1", name: "Alpha" }, { id: "e2", name: "Bravo" }],
  latest_revision: { id: "r1", revision_number: 1 }, trigger: { kind: "manual", due_at: null },
};
const PREVIEW = {
  campaign_id: "c1",
  revision: { id: "r1", revision_number: 1, subject: "Welcome", body: "Read the **rules**", content_profile: "p", content_digest: "sha256:x" },
  channels: [{ channel: "in_app", available: true }], acknowledgement_policy: "none",
  trigger: { kind: "manual", due_at: null },
  events: [
    { event_id: "e1", name: "Alpha", event_timezone: "UTC", recipient_count: 2, local_due_at: null },
    { event_id: "e2", name: "Bravo", event_timezone: "America/New_York", recipient_count: 1, local_due_at: null },
  ],
  recipient_total: 3, audience_cap: 5000, exceeds_audience_cap: false, observed_at: "2026-10-01T00:00:00Z",
};

function stubApi(route: (url: string, method: string) => Reply | undefined) {
  const fetch = vi.fn().mockImplementation(async (url: string, init: RequestInit = {}) => {
    const [status, body] = route(url, init.method ?? "GET") ?? [404, { error: { code: "not_found", message: "Missing" } }];
    return { status, ok: status < 400, text: async () => JSON.stringify(body) };
  });
  vi.stubGlobal("fetch", fetch);
  return fetch;
}

const posts = (fetch: ReturnType<typeof vi.fn>, suffix: string) =>
  fetch.mock.calls.filter(([url, init]) => init?.method === "POST" && String(url).endsWith(suffix));

it("composes with the keyboard, previews, and retries the same reviewed release", async () => {
  let releaseAttempts = 0;
  const fetch = stubApi((url, method) => {
    if (url.startsWith("/api/v1/ctf/communications/targets/")) return [200, TARGETS];
    if (url === "/api/v1/ctf/communications/" && method === "POST") return [201, CAMPAIGN];
    if (url.startsWith("/api/v1/ctf/communications/?")) return [200, { results: [], next_offset: null }];
    if (url.startsWith("/api/v1/ctf/communications/c1/preview/")) return [200, PREVIEW];
    if (url === "/api/v1/ctf/communications/c1/release/") {
      releaseAttempts += 1;
      return releaseAttempts === 1
        ? [503, { error: { code: "unavailable", message: "Delivery is temporarily unavailable." } }]
        : [202, { id: "i1", status: "released", due_at: null, released_at: "2026-10-01T00:00:00Z" }];
    }
    return undefined;
  });
  const user = setupUser();
  renderRoute(<CommunicationsPage />, { path: "/ctf/admin/communications", initialEntries: ["/ctf/admin/communications"] });

  const alpha = await screen.findByRole("checkbox", { name: /Alpha/ });
  alpha.focus();
  await user.keyboard(" ");
  await user.tab();
  await user.keyboard(" ");
  expect(screen.getByRole("checkbox", { name: /Bravo/ })).toBeChecked();
  await user.type(screen.getByLabelText("Title (organizers only)"), "Kickoff");
  await user.type(screen.getByLabelText("Subject"), "Welcome");
  await user.type(screen.getByLabelText("Message (Markdown)"), "Read the rules{Enter}");
  await user.click(screen.getByRole("button", { name: "Save draft and preview" }));

  const preview = await screen.findByRole("region", { name: "Preview" });
  expect(within(preview).getByText("Welcome")).toBeInTheDocument();
  expect(within(preview).getByText(/Preview sends nothing/)).toBeInTheDocument();
  const draft = JSON.parse(posts(fetch, "/communications/")[0][1].body);
  expect(draft.target_event_ids).toEqual(["e1", "e2"]);
  expect(draft.audience_spec).toEqual({ kind: "multi_event", event_ids: ["e1", "e2"] });
  expect(screen.getByRole("checkbox", { name: /Alpha/ })).toBeDisabled();

  await user.click(screen.getByRole("button", { name: "Send now" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("Delivery is temporarily unavailable.");
  await user.click(screen.getByRole("button", { name: "Send now" }));
  await waitFor(() => expect(releaseAttempts).toBe(2));
  const releases = posts(fetch, "/c1/release/");
  expect(JSON.parse(releases[0][1].body)).toEqual({ occurrence_key: "initial", revision_id: "r1" });
  expect(releases[1][1].body).toBe(releases[0][1].body);
  expect(posts(fetch, "/api/v1/ctf/communications/")).toHaveLength(1);
});

it("refuses a scheduled time that a clock change skips", async () => {
  stubApi((url) => {
    if (url.startsWith("/api/v1/ctf/communications/targets/")) return [200, TARGETS];
    if (url.startsWith("/api/v1/ctf/communications/?")) return [200, { results: [], next_offset: null }];
    return undefined;
  });
  const user = setupUser();
  renderRoute(<CommunicationsPage />, { path: "/ctf/admin/communications", initialEntries: ["/ctf/admin/communications"] });
  await user.click(await screen.findByRole("checkbox", { name: /Bravo/ }));
  await user.type(screen.getByLabelText("Title (organizers only)"), "T");
  await user.type(screen.getByLabelText("Subject"), "S");
  await user.type(screen.getByLabelText("Message (Markdown)"), "B");
  await user.click(screen.getByRole("radio", { name: "Schedule" }));
  await user.selectOptions(screen.getByLabelText("Time zone"), "America/New_York");
  await user.type(screen.getByLabelText("Date"), "2099-03-08");
  await user.type(screen.getByLabelText("Time"), "02:30");
  expect(await screen.findByText(/does not exist in the selected time zone/)).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Save draft and preview" })).toBeDisabled();
});

it("shows delivery state, recipients, activity, and cancels through a confirmation", async () => {
  const report = {
    results: [
      {
        intent_id: "i1", status: "scheduled", trigger_kind: "absolute_time", revision_number: 1,
        due_at: "2099-01-01T00:00:00Z", released_at: null, schedule_status: "failed",
        events: [{ event_id: "e1", name: "Alpha", recipients: 2, inbox_available: 2, read: 1, acknowledged: 0,
          channels: [{ channel: "in_app", queued: 1, claimed: 0, retry_due: 0, accepted: 1, permanent_failure: 0, cancelled: 0, suppressed: 0, expired: 0 }] }],
        totals: { recipients: 2, inbox_available: 2, read: 1, acknowledged: 0,
          channels: [{ channel: "in_app", queued: 1, claimed: 0, retry_due: 0, accepted: 1, permanent_failure: 0, cancelled: 0, suppressed: 0, expired: 0 }] },
      },
    ],
    next_offset: null,
  };
  const fetch = stubApi((url, method) => {
    if (url === "/api/v1/ctf/communications/c1/") return [200, CAMPAIGN];
    if (url.startsWith("/api/v1/ctf/communications/c1/delivery/")) return [200, report];
    if (url.startsWith("/api/v1/ctf/communications/c1/activity/"))
      return [200, { entries: [{ kind: "created", at: "2026-10-01T00:00:00Z", actor: { label: "alice", via: "session" }, revision_number: null, intent_id: null, status: null, due_at: null }], truncated: false }];
    if (url.startsWith("/api/v1/ctf/communications/c1/recipients/"))
      return [200, { results: [{ participant_id: "p1", name: "Ada", team: null, removed: false, inbox_available: true, read_at: null, acknowledged_at: null, channels: [{ channel: "in_app", status: "accepted" }] }], next_offset: null }];
    if (url === "/api/v1/ctf/communications/c1/cancel/" && method === "POST") return [200, { ...CAMPAIGN, status: "cancelled" }];
    return undefined;
  });
  const user = setupUser();
  renderRoute(<CommunicationDetailPage />, {
    path: "/ctf/admin/communications/:campaignId",
    initialEntries: ["/ctf/admin/communications/c1"],
  });

  expect(await screen.findByText("Release task failed")).toBeInTheDocument();
  expect(screen.getByText(/2 recipients · 2 in inbox · 1 read · 0 acknowledged/)).toBeInTheDocument();
  const activity = screen.getByRole("region", { name: "Activity" });
  expect(await within(activity).findByRole("listitem")).toHaveTextContent("created by alice via session");

  await user.click(screen.getByRole("button", { name: "Show recipients for Alpha" }));
  expect(await screen.findByText("Ada")).toBeInTheDocument();

  const trigger = screen.getByRole("button", { name: "Cancel unclaimed work" });
  trigger.focus();
  await user.keyboard("{Enter}");
  const dialog = await screen.findByRole("alertdialog");
  expect(dialog).toHaveTextContent(/stay delivered/);
  await user.keyboard("{Escape}");
  await waitFor(() => expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument());
  expect(posts(fetch, "/c1/cancel/")).toHaveLength(0);

  await user.click(screen.getByRole("button", { name: "Cancel unclaimed work" }));
  await user.click(within(await screen.findByRole("alertdialog")).getByRole("button", { name: "Cancel unclaimed work" }));
  await waitFor(() => expect(posts(fetch, "/c1/cancel/")).toHaveLength(1));
});


async function composeDraft(user: ReturnType<typeof setupUser>) {
  await user.click(await screen.findByRole("checkbox", { name: /Alpha/ }));
  await user.type(screen.getByLabelText("Title (organizers only)"), "Kickoff");
  await user.type(screen.getByLabelText("Subject"), "Welcome");
  await user.type(screen.getByLabelText("Message (Markdown)"), "Read the rules");
  await user.click(screen.getByRole("button", { name: "Save draft and preview" }));
}

it("keeps release disabled until the preview of the same revision succeeds", async () => {
  let previewCalls = 0;
  stubApi((url, method) => {
    if (url.startsWith("/api/v1/ctf/communications/targets/")) return [200, TARGETS];
    if (url === "/api/v1/ctf/communications/" && method === "POST") return [201, CAMPAIGN];
    if (url.startsWith("/api/v1/ctf/communications/?")) return [200, { results: [], next_offset: null }];
    if (url.startsWith("/api/v1/ctf/communications/c1/preview/")) {
      previewCalls += 1;
      return previewCalls === 1 ? [503, { error: { code: "unavailable", message: "Preview unavailable" } }] : [200, PREVIEW];
    }
    return undefined;
  });
  const user = setupUser();
  renderRoute(<CommunicationsPage />, { path: "/ctf/admin/communications", initialEntries: ["/ctf/admin/communications"] });
  await composeDraft(user);
  expect(await screen.findByText("Could not load the preview")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Send now" })).toBeDisabled();
  await user.click(screen.getByRole("button", { name: "Retry preview" }));
  await screen.findByRole("region", { name: "Preview" });
  expect(screen.getByRole("button", { name: "Send now" })).toBeEnabled();
});

it("loads further pages of events and keeps the selection", async () => {
  const fetch = stubApi((url) => {
    const query = new URL(url, "http://localhost").searchParams;
    if (url.startsWith("/api/v1/ctf/communications/targets/")) {
      return query.get("offset") === "100"
        ? [200, { ...TARGETS, events: [{ ...TARGETS.events[0], id: "e3", name: "Charlie" }], next_offset: null }]
        : [200, { ...TARGETS, next_offset: 100 }];
    }
    if (url.startsWith("/api/v1/ctf/communications/?")) return [200, { results: [], next_offset: null }];
    return undefined;
  });
  const user = setupUser();
  renderRoute(<CommunicationsPage />, { path: "/ctf/admin/communications", initialEntries: ["/ctf/admin/communications"] });
  await user.click(await screen.findByRole("checkbox", { name: /Alpha/ }));
  await user.click(screen.getByRole("button", { name: "Load more events" }));
  expect(await screen.findByRole("checkbox", { name: /Charlie/ })).not.toBeChecked();
  expect(screen.getByRole("checkbox", { name: /Alpha/ })).toBeChecked();
  expect(fetch.mock.calls.some(([url]) => String(url).includes("offset=100"))).toBe(true);
});

it("loads older delivery occurrences", async () => {
  const occurrence = (id: string, revision: number) => ({
    intent_id: id, status: "released", trigger_kind: "manual", revision_number: revision, due_at: null,
    released_at: "2026-10-01T00:00:00Z", schedule_status: null, events: [],
    totals: { recipients: 0, inbox_available: 0, read: 0, acknowledged: 0, channels: [] },
  });
  stubApi((url) => {
    const query = new URL(url, "http://localhost").searchParams;
    if (url === "/api/v1/ctf/communications/c1/") return [200, CAMPAIGN];
    if (url.startsWith("/api/v1/ctf/communications/c1/delivery/")) {
      return query.get("offset") === "25"
        ? [200, { results: [occurrence("i0", 1)], next_offset: null }]
        : [200, { results: [occurrence("i9", 2)], next_offset: 25 }];
    }
    if (url.startsWith("/api/v1/ctf/communications/c1/activity/")) return [200, { entries: [], truncated: false }];
    return undefined;
  });
  const user = setupUser();
  renderRoute(<CommunicationDetailPage />, {
    path: "/ctf/admin/communications/:campaignId",
    initialEntries: ["/ctf/admin/communications/c1"],
  });
  expect(await screen.findByText("Revision 2")).toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "Load older occurrences" }));
  expect(await screen.findByText("Revision 1")).toBeInTheDocument();
});
