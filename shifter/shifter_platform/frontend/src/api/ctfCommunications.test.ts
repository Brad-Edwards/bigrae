import { afterEach, expect, it, vi } from "vitest";
import { draftBody, submitCommunication } from "./ctfCommunications";

afterEach(() => vi.unstubAllGlobals());

it.each(["account", "organization", "workspace"] as const)("writes the exact %s campaign parent", (kind) => {
  const body = draftBody({ parent: { kind, id: "parent-1" }, eventIds: ["event-1"], title: "Title", subject: "Subject", body: "Message" });
  expect(body).toMatchObject({ [kind]: "parent-1", target_event_ids: ["event-1"] });
  expect(body).not.toHaveProperty("workspace_id");
  for (const other of ["account", "organization", "workspace"].filter((value) => value !== kind)) {
    expect(body).not.toHaveProperty(other);
  }
});

it.each(["account", "organization", "workspace"] as const)("retries a %s release against the same reviewed revision after a lost response", async (kind) => {
  const responses = [
    [200, { [kind]: "parent-1" }], [201, { id: "campaign-1", latest_revision: { id: "revision-1" } }],
    [503, { error: { code: "unavailable", message: "Retry" } }],
    [202, { id: "intent-1", status: "released" }],
  ];
  const fetch = vi.fn().mockImplementation(async () => {
    const [status, body] = responses.shift()!;
    return { status, ok: Number(status) < 400, text: async () => JSON.stringify(body) };
  });
  vi.stubGlobal("fetch", fetch);
  const pending = { current: null as { fingerprint: string; campaignId: string } | null };
  const input = { subject: "Welcome", body: "Read the rules" };
  await expect(submitCommunication("event-1", input, pending)).rejects.toThrow();
  await submitCommunication("event-1", input, pending);
  const writes = fetch.mock.calls.filter(([, init]) => init.method === "POST");
  expect(writes.map(([url]) => url)).toEqual([
    "/api/v1/ctf/communications/", "/api/v1/ctf/communications/campaign-1/release/",
    "/api/v1/ctf/communications/campaign-1/release/",
  ]);
  expect(writes[1][1].body).toBe(writes[2][1].body);
  expect(JSON.parse(writes[1][1].body)).toEqual({ occurrence_key: "initial", revision_id: "revision-1" });
  expect(JSON.parse(writes[0][1].body)).toHaveProperty(kind, "parent-1");
  expect(JSON.parse(writes[0][1].body).channels).toEqual(["in_app"]);
});

it("preserves the generated discriminator contracts", () => {
  type Audience = import("./schema").components["schemas"]["CommunicationAudience"];
  type Trigger = import("./schema").components["schemas"]["CommunicationTrigger"];
  // @ts-expect-error A team audience cannot select participant IDs.
  const wrongAudience: Audience = { kind: "team", participant_ids: ["p1"] };
  // @ts-expect-error An absolute-time trigger requires due_at.
  const missingTime: Trigger = { kind: "absolute_time" };
  expect(wrongAudience.kind).toBe("team");
  expect(missingTime.kind).toBe("absolute_time");
});
