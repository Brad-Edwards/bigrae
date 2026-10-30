import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, screen } from "@testing-library/react";

import { renderRoute } from "@/test/utils";

vi.mock("@/api/client", () => ({ apiFetch: vi.fn() }));

import { apiFetch } from "@/api/client";

import { MonitoringPage } from "./MonitoringPage";

const mockApi = vi.mocked(apiFetch);

function render() {
  return renderRoute(<MonitoringPage defaultTab="scoreboard" />, {
    path: "/ctf/admin/events/:eventId/monitoring",
    initialEntries: ["/ctf/admin/events/e1/monitoring"],
  });
}

beforeEach(() => {
  mockApi.mockReset();
});

describe("MonitoringPage", () => {
  it("renders the tabs and the default scoreboard content", async () => {
    mockApi.mockResolvedValue({
      team_mode: false,
      frozen: false,
      rankings: [{ participant_id: "p1", name: "Ada Lovelace", score: 300, solve_count: 3, rank: 1 }],
    });
    render();
    expect(await screen.findByText("Ada Lovelace")).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Scoreboard" })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Ranges" })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Alerts" })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Notifications" })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Analytics" })).toBeInTheDocument();
  });

  it("shows the scoreboard empty state", async () => {
    mockApi.mockResolvedValue({ team_mode: false, frozen: false, rankings: [] });
    render();
    expect(await screen.findByText("No scores yet")).toBeInTheDocument();
  });

  it("loads the scoreboard from the organizer-scoreboard endpoint", async () => {
    mockApi.mockImplementation((path: string) =>
      path === "/ctf/events/e1/organizer-scoreboard/"
        ? Promise.resolve({
            team_mode: false,
            frozen: false,
            rankings: [{ participant_id: "p1", name: "Ada Lovelace", score: 300, solve_count: 3, rank: 1 }],
          })
        : Promise.resolve({}),
    );
    render();
    expect(await screen.findByText("Ada Lovelace")).toBeInTheDocument();
    // The organizer monitoring board must use the full-visibility organizer read,
    // not the public `/ctf/events/<id>/scoreboard/` hook (which honors freeze/hide).
    expect(mockApi).toHaveBeenCalledWith("/ctf/events/e1/organizer-scoreboard/", expect.anything());
  });

  it("reports a results export that fails", async () => {
    mockApi.mockImplementation((path: string) => {
      if (path.endsWith("/results/export/")) return Promise.reject(new TypeError("Failed to fetch"));
      // No analytics yet: the analytics card renders nothing for a null body.
      if (path.endsWith("/analytics/")) return Promise.resolve(null);
      return Promise.resolve({});
    });
    renderRoute(<MonitoringPage defaultTab="analytics" />, {
      path: "/ctf/admin/events/:eventId/monitoring",
      initialEntries: ["/ctf/admin/events/e1/monitoring"],
    });
    fireEvent.click(await screen.findByRole("button", { name: "Export results" }));
    expect(await screen.findByText("Could not export the results.")).toBeInTheDocument();
    expect(screen.queryByText("Failed to fetch")).not.toBeInTheDocument();
  });
});
