import { beforeEach, describe, expect, it, vi } from "vitest";
import { screen, waitFor } from "@testing-library/react";

import { renderRoute, setupUser } from "@/test/utils";

vi.mock("@/api/client", () => ({ apiFetch: vi.fn() }));

import { apiFetch } from "@/api/client";

import { EventFormPage } from "./EventFormPage";

const mockApi = vi.mocked(apiFetch);

const EVENT = {
  id: "e1",
  name: "Spring CTF",
  description: "A spring event",
  public_registration_enabled: true,
  public_registration_url: "https://ctf.example.test/ctf/public/events/e1/",
  status: "registration",
  event_start: "2026-08-01T10:00:00Z",
  event_end: "2026-08-01T18:00:00Z",
  registration_deadline: null,
  scenario_id: "",
  auto_cleanup: true,
  cleanup_delay_hours: 24,
  max_participants: null,
  team_mode: false,
  team_size_limit: null,
  range_config: {},
  range_spinup_minutes: 30,
  submission_cooldown_seconds: 0,
  attempt_limit_mode: "none",
  attempt_limit_cooldown_seconds: 0,
  rating_visibility: "hidden",
  scoring_mode: "static",
  scoreboard_visible: true,
  scoreboard_freeze_at: null,
};

function routeApi(handlers: (path: string, options?: { method?: string }) => unknown) {
  mockApi.mockImplementation((path: string, options?: { method?: string }) => Promise.resolve(
    path === "/cms/content-scopes/"
      ? { count: 2, results: [
          { kind: "installation", name: "Shared catalog", scope_kind: "installation", account: null, organization: null, workspace: null },
          { kind: "account", name: "Synthetic tenant", scope_kind: "account", account: "11111111-1111-4111-8111-111111111111", organization: null, workspace: null },
        ] }
      : path === "/cms/model-source-options/"
      ? { available: false, workspace: null, workspaces: [], aliases: [] }
      : handlers(path, options),
  ));
}

beforeEach(() => {
  mockApi.mockReset();
});

describe("EventFormPage (create)", () => {
  it("loads scenarios from the selected explicit catalog", async () => {
    routeApi((path) => path === "/ctf/scenario-options/" ? { scenarios: [] } : {});
    const user = setupUser();
    renderRoute(<EventFormPage mode="create" />, { path: "/*", initialEntries: ["/ctf/admin/events/create"] });
    const catalog = await screen.findByRole("combobox", { name: "Scenario catalog" });
    await waitFor(() => expect(mockApi).toHaveBeenCalledWith("/ctf/scenario-options/", expect.objectContaining({
      query: { scope_kind: "installation", account: null, organization: null, workspace: null },
    })));
    await user.selectOptions(catalog, JSON.stringify(["account", "11111111-1111-4111-8111-111111111111", null, null]));
    await waitFor(() => expect(mockApi).toHaveBeenCalledWith("/ctf/scenario-options/", expect.objectContaining({
      query: { scope_kind: "account", account: "11111111-1111-4111-8111-111111111111", organization: null, workspace: null },
    })));
  });

  it("submits a create request via POST /ctf/events/", async () => {
    routeApi((path) => {
      if (path === "/ctf/scenario-options/") return { scenarios: [{ id: "s1", name: "Basic" }] };
      if (path === "/ctf/events/") return { id: "e9", name: "New", status: "draft" };
      return {};
    });
    const user = setupUser();
    renderRoute(<EventFormPage mode="create" />, { path: "/*", initialEntries: ["/ctf/admin/events/create"] });

    expect(await screen.findByRole("checkbox", { name: "Publish public registration page" })).not.toBeChecked();
    await user.type(await screen.findByLabelText("Name"), "New event");
    await user.click(screen.getByRole("button", { name: "Create event" }));

    await waitFor(() => {
      const call = mockApi.mock.calls.find(([p, o]) => p === "/ctf/events/" && o?.method === "POST");
      expect(call).toBeDefined();
      expect(call?.[1]?.body).toMatchObject({ public_registration_enabled: false });
    });
  });

  it("shows authored copy when the save fails without an API error", async () => {
    routeApi((path, options) => {
      if (path === "/ctf/scenarios/") return { scenarios: [] };
      if (path === "/ctf/events/" && options?.method === "POST") {
        return Promise.reject(new TypeError("Failed to fetch"));
      }
      return {};
    });
    const user = setupUser();
    renderRoute(<EventFormPage mode="create" />, { path: "/*", initialEntries: ["/ctf/admin/events/create"] });

    await user.type(await screen.findByLabelText("Name"), "New event");
    await user.click(screen.getByRole("button", { name: "Create event" }));

    expect(await screen.findByText("Could not save")).toBeInTheDocument();
    expect(screen.getByText("The event could not be saved.")).toBeInTheDocument();
    expect(screen.queryByText("Failed to fetch")).not.toBeInTheDocument();
  });
});

describe("EventFormPage (edit)", () => {
  it("loads the existing event into the form", async () => {
    routeApi((path) => {
      if (path === "/ctf/scenario-options/") return { scenarios: [] };
      if (path === "/ctf/events/e1/") return EVENT;
      return {};
    });
    renderRoute(<EventFormPage mode="edit" />, {
      path: "/ctf/admin/events/:eventId/edit",
      initialEntries: ["/ctf/admin/events/e1/edit"],
    });

    expect(await screen.findByDisplayValue("Spring CTF")).toBeInTheDocument();
    expect(screen.getByRole("checkbox", { name: "Publish public registration page" })).toBeChecked();
    expect(screen.getByText(/publishes the event name, description, dates, and registration deadline/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Save changes" })).toBeInTheDocument();
  });
});
