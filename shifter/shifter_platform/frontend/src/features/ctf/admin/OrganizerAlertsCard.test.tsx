import { beforeEach, describe, expect, it, vi } from "vitest";
import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { renderRoute } from "@/test/utils";

vi.mock("@/api/client", () => ({ apiFetch: vi.fn() }));

import { apiFetch } from "@/api/client";

import { OrganizerAlertsCard } from "./OrganizerAlertsCard";

const mockApi = vi.mocked(apiFetch);

const ALERT = {
  message_id: "a1",
  subject: "Participant ranges are not ready: Spring CTF",
  body: "At the scheduled start time, 1 of 4 participant ranges were ready.",
  content_profile: "ctf-communication-markdown/v1",
  origin: "system_milestone",
  acknowledgement_policy: "none",
  read_at: null,
  acknowledged_at: null,
  dismissed_at: null,
  action_required: false,
  created_at: "2026-10-30T10:00:00Z",
};

function page(results: unknown[], unread = 1) {
  return { results, next_offset: null, unread_count: unread, action_required_count: 0, allowed_link_hosts: [] };
}

beforeEach(() => {
  mockApi.mockReset();
});

describe("OrganizerAlertsCard", () => {
  it("lists alerts with the unread count and the alert text", async () => {
    mockApi.mockResolvedValue(page([ALERT]));
    renderRoute(<OrganizerAlertsCard eventId="e1" />);
    expect(await screen.findByText(ALERT.subject)).toBeInTheDocument();
    expect(screen.getByText(/1 of 4 participant ranges were ready/)).toBeInTheDocument();
    expect(screen.getByText("1 unread")).toBeInTheDocument();
    expect(mockApi).toHaveBeenCalledWith(
      "/ctf/events/e1/organizer-alerts/",
      expect.objectContaining({ query: expect.objectContaining({ view: "inbox" }) }),
    );
  });

  it("marks an alert read and dismisses it", async () => {
    mockApi.mockImplementation(async (_path: string, options?: { method?: string }) =>
      options?.method === "POST" ? ALERT : page([ALERT]),
    );
    renderRoute(<OrganizerAlertsCard eventId="e1" />);
    await userEvent.click(await screen.findByRole("button", { name: "Mark as read" }));
    expect(mockApi).toHaveBeenCalledWith(
      "/ctf/events/e1/organizer-alerts/a1/read/",
      expect.objectContaining({ method: "POST", body: {} }),
    );
    await userEvent.click(screen.getByRole("button", { name: "Dismiss" }));
    expect(mockApi).toHaveBeenCalledWith(
      "/ctf/events/e1/organizer-alerts/a1/dismiss/",
      expect.objectContaining({ method: "POST", body: {} }),
    );
  });

  it("shows dismissed alerts on request", async () => {
    mockApi.mockResolvedValue(page([]));
    renderRoute(<OrganizerAlertsCard eventId="e1" />);
    expect(await screen.findByText("No alerts.")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Show dismissed" }));
    expect(mockApi).toHaveBeenCalledWith(
      "/ctf/events/e1/organizer-alerts/",
      expect.objectContaining({ query: expect.objectContaining({ view: "dismissed" }) }),
    );
  });

  it("reports an unavailable inbox without the raw error", async () => {
    mockApi.mockRejectedValue(new TypeError("Failed to fetch"));
    renderRoute(<OrganizerAlertsCard eventId="e1" />);
    expect(await screen.findByText("Alerts are unavailable.")).toBeInTheDocument();
    expect(screen.queryByText("Failed to fetch")).not.toBeInTheDocument();
  });
});
