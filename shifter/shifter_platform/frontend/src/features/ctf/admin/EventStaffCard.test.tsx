import { beforeEach, describe, expect, it, vi } from "vitest";
import { screen } from "@testing-library/react";

import { renderRoute, setupUser } from "@/test/utils";

vi.mock("@/api/client", () => ({ apiFetch: vi.fn() }));

import { apiFetch } from "@/api/client";

import { EventStaffCard } from "./EventStaffCard";

const mockApi = vi.mocked(apiFetch);
const moderatorPrincipal = "11111111-1111-4111-8111-111111111111";
const judgePrincipal = "22222222-2222-4222-8222-222222222222";
const coOrganizerPrincipal = "33333333-3333-4333-8333-333333333333";

beforeEach(() => {
  mockApi.mockReset();
});

describe("EventStaffCard", () => {
  it("lists assigned staff with roles", async () => {
    mockApi.mockResolvedValue({
      staff: [
        { principal_uuid: moderatorPrincipal, role: "moderator", created_at: null },
        { principal_uuid: judgePrincipal, role: "judge", created_at: null },
      ],
    });
    renderRoute(<EventStaffCard eventId="e1" />);
    expect(await screen.findByText(moderatorPrincipal)).toBeInTheDocument();
    expect(screen.getByText("· judge")).toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: "Remove" })).toHaveLength(2);
  });

  it("assigns a staff member by principal UUID", async () => {
    const user = setupUser();
    mockApi.mockResolvedValue({ staff: [] });
    renderRoute(<EventStaffCard eventId="e1" />);
    await screen.findByText("No staff assigned.");
    await user.type(screen.getByLabelText("Principal UUID"), moderatorPrincipal);
    await user.click(screen.getByRole("button", { name: "Add staff" }));
    expect(mockApi).toHaveBeenCalledWith(
      "/ctf/events/e1/staff-members/",
      expect.objectContaining({
        method: "POST",
        body: { principal_uuid: moderatorPrincipal, role: "moderator" },
      }),
    );
  });

  it("revokes a staff member", async () => {
    const user = setupUser();
    mockApi.mockResolvedValue({
      staff: [{ principal_uuid: moderatorPrincipal, role: "moderator", created_at: null }],
    });
    renderRoute(<EventStaffCard eventId="e1" />);
    await user.click(await screen.findByRole("button", { name: "Remove" }));
    expect(mockApi).toHaveBeenCalledWith(
      `/ctf/events/e1/staff-members/${moderatorPrincipal}/`,
      expect.objectContaining({ method: "DELETE" }),
    );
  });

  it("assigns a co-organizer (#1922)", async () => {
    const user = setupUser();
    mockApi.mockResolvedValue({ staff: [] });
    renderRoute(<EventStaffCard eventId="e1" />);
    await screen.findByText("No staff assigned.");
    await user.type(screen.getByLabelText("Principal UUID"), coOrganizerPrincipal);
    await user.click(screen.getByLabelText("Role"));
    await user.click(await screen.findByRole("option", { name: "Co-organizer" }));
    await user.click(screen.getByRole("button", { name: "Add staff" }));
    expect(mockApi).toHaveBeenCalledWith(
      "/ctf/events/e1/staff-members/",
      expect.objectContaining({
        method: "POST",
        body: { principal_uuid: coOrganizerPrincipal, role: "co_organizer" },
      }),
    );
  });

  it("offers 'Make owner' only for co-organizers and transfers ownership (#1922)", async () => {
    const user = setupUser();
    mockApi.mockResolvedValue({
      staff: [
        { principal_uuid: moderatorPrincipal, role: "moderator", created_at: null },
        { principal_uuid: coOrganizerPrincipal, role: "co_organizer", created_at: null },
      ],
    });
    renderRoute(<EventStaffCard eventId="e1" />);
    await screen.findByText(coOrganizerPrincipal);
    // Exactly one "Make owner" button, for the co-organizer row.
    const makeOwner = screen.getAllByRole("button", { name: "Make owner" });
    expect(makeOwner).toHaveLength(1);
    await user.click(makeOwner[0]);
    expect(mockApi).toHaveBeenCalledWith(
      "/ctf/events/e1/ownership/transfer/",
      expect.objectContaining({ method: "POST", body: { principal_uuid: coOrganizerPrincipal } }),
    );
  });

  it("renders nothing for a non-owner (owner-only surface, #1922)", () => {
    const { container } = renderRoute(<EventStaffCard eventId="e1" canManage={false} />);
    expect(container).toBeEmptyDOMElement();
    expect(mockApi).not.toHaveBeenCalled();
  });

  it.each(["assign", "revoke", "transfer"])("retries the retained %s command without changing its target", async (kind) => {
    const user = setupUser();
    mockApi.mockResolvedValue({staff: [], pending_transition: {
      id: "pending-id", kind, target_principal_uuid: coOrganizerPrincipal, requested_role: "judge",
      created_at: "2026-10-28T12:00:00Z", total_effects: 2, completed_effects: 1,
    }});
    renderRoute(<EventStaffCard eventId="e1" />);
    await user.click(await screen.findByRole("button", {name: "Retry pending change"}));
    const path = kind === "transfer" ? "/ctf/events/e1/ownership/transfer/" :
      kind === "revoke" ? `/ctf/events/e1/staff-members/${coOrganizerPrincipal}/` : "/ctf/events/e1/staff-members/";
    const request = kind === "revoke" ? {method: "DELETE"} : {method: "POST", body: {
      principal_uuid: coOrganizerPrincipal, ...(kind === "assign" ? {role: "judge"} : {}),
    }};
    expect(mockApi).toHaveBeenCalledWith(path, expect.objectContaining(request));
    expect(screen.getByRole("button", {name: "Add staff"})).toBeDisabled();
  });
});
