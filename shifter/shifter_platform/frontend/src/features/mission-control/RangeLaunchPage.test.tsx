import { beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { axe } from "vitest-axe";

import { ApiError } from "@/api/errors";
import type { AgentListResponse, ScenarioListResponse } from "@/api/types";
import { renderRoute } from "@/test/utils";

const navigateMock = vi.fn();
vi.mock("react-router", async (importOriginal) => {
  const actual = await importOriginal<typeof import("react-router")>();
  return { ...actual, useNavigate: () => navigateMock };
});

vi.mock("@/api/client", () => ({ apiFetch: vi.fn() }));

import { apiFetch } from "@/api/client";

import { RangeLaunchPage } from "./RangeLaunchPage";

const mockApi = vi.mocked(apiFetch);

const SCENARIOS: ScenarioListResponse = {
  scenarios: [
    {
      id: "basic",
      name: "Basic Range",
      description: "A basic single-host range.",
      enabled: true,
      is_default: true,
      staff_only: false,
      launchable: true,
    },
  ],
};

const AGENTS: AgentListResponse = {
  agents: [
    {
      id: 5,
      name: "kali-agent",
      os_name: "Kali Linux",
      os_slug: "kali",
      file_size_mb: 12.3,
      original_filename: "kali.tar.gz",
      created_at: "2026-07-01T00:00:00Z",
      agent_type: "xdr",
      agent_type_display: "XDR",
    },
  ],
  max_file_size_bytes: 2048 * 1024 * 1024,
};

const LAUNCH_WORKSPACES = {
  count: 1,
  personal_available: true,
  workspaces: [
    {
      uuid: "11111111-1111-4111-8111-111111111111",
      name: "Range lab",
      organization_uuid: "22222222-2222-4222-8222-222222222222",
      organization_name: "Example organization",
    },
  ],
};

function mockOptions() {
  mockApi.mockImplementation((path: string) => {
    if (path === "/mission-control/launch-workspaces/") return Promise.resolve(LAUNCH_WORKSPACES);
    if (path === "/mission-control/scenarios/") return Promise.resolve(SCENARIOS);
    if (path === "/mission-control/agents/") return Promise.resolve(AGENTS);
    return Promise.reject(new Error(`unexpected path ${path}`));
  });
}

async function selectOption(user: ReturnType<typeof userEvent.setup>, triggerName: string, optionName: string) {
  await user.click(await screen.findByRole("combobox", { name: triggerName }));
  await user.click(await screen.findByRole("option", { name: optionName }));
}

beforeAll(() => {
  // Radix `Select` needs pointer-capture/scroll APIs jsdom does not implement.
  window.HTMLElement.prototype.hasPointerCapture = vi.fn();
  window.HTMLElement.prototype.releasePointerCapture = vi.fn();
  window.HTMLElement.prototype.scrollIntoView = vi.fn();
});

beforeEach(() => {
  mockApi.mockReset();
  navigateMock.mockReset();
});

describe("RangeLaunchPage", () => {
  it("discovers launch workspaces before any scenario is selected", async () => {
    mockOptions();
    renderRoute(<RangeLaunchPage />);

    expect(await screen.findByRole("option", { name: "Example organization / Range lab" })).toBeInTheDocument();
    expect(mockApi).toHaveBeenCalledWith("/mission-control/launch-workspaces/", expect.objectContaining({ signal: expect.anything() }));
  });

  it("reloads catalog options for the selected workspace", async () => {
    mockOptions();
    const user = userEvent.setup();
    renderRoute(<RangeLaunchPage />);

    await user.selectOptions(await screen.findByLabelText("Workspace"), LAUNCH_WORKSPACES.workspaces[0].uuid);

    await waitFor(() =>
      expect(mockApi).toHaveBeenCalledWith(
        "/mission-control/scenarios/",
        expect.objectContaining({ query: { workspace_uuid: LAUNCH_WORKSPACES.workspaces[0].uuid } }),
      ),
    );
    expect(mockApi).toHaveBeenCalledWith(
      "/mission-control/agents/",
      expect.objectContaining({ query: { workspace_uuid: LAUNCH_WORKSPACES.workspaces[0].uuid } }),
    );
  });

  it("shows a loading skeleton while launch options load", () => {
    mockApi.mockReturnValue(new Promise(() => {}));
    const { container } = renderRoute(<RangeLaunchPage />);
    expect(container.querySelectorAll('[data-slot="skeleton"]').length).toBeGreaterThan(0);
  });

  it("shows an error when launch options fail to load", async () => {
    mockApi.mockRejectedValue(new ApiError(500, { code: "error", message: "boom" }));
    renderRoute(<RangeLaunchPage />);
    expect(await screen.findByText("Could not load launch options")).toBeInTheDocument();
  });

  it("requires a scenario and an agent before launching", async () => {
    mockOptions();
    const user = userEvent.setup();
    renderRoute(<RangeLaunchPage />);

    await screen.findByRole("button", { name: "Launch range" });
    await user.click(screen.getByRole("button", { name: "Launch range" }));

    expect(await screen.findByText("Select a scenario to launch.")).toBeInTheDocument();
    expect(screen.getByText("Select an agent to launch.")).toBeInTheDocument();
    expect(mockApi).not.toHaveBeenCalledWith("/mission-control/range/launch/", expect.anything());
  });

  it("launches the range and navigates to the dashboard on success", async () => {
    mockOptions();
    const user = userEvent.setup();
    renderRoute(<RangeLaunchPage />);

    await selectOption(user, "Scenario", "Basic Range");
    await selectOption(user, "Agent", "kali-agent (Kali Linux)");

    mockApi.mockImplementation((path: string) => {
      if (path === "/mission-control/range/launch/") return Promise.resolve({ success: true, range: {} });
      if (path === "/mission-control/launch-workspaces/") return Promise.resolve(LAUNCH_WORKSPACES);
      if (path === "/mission-control/scenarios/") return Promise.resolve(SCENARIOS);
      if (path === "/mission-control/agents/") return Promise.resolve(AGENTS);
      return Promise.reject(new Error("unexpected"));
    });

    await user.click(screen.getByRole("button", { name: "Launch range" }));

    await waitFor(() =>
      expect(mockApi).toHaveBeenCalledWith("/mission-control/range/launch/", {
        method: "POST",
        body: { scenario: "basic", agent_id: 5 },
      }),
    );
    await waitFor(() => expect(navigateMock).toHaveBeenCalledWith("/mission-control/"));
  });

  it("shows a server error without retrying automatically", async () => {
    mockOptions();
    const user = userEvent.setup();
    renderRoute(<RangeLaunchPage />);

    await selectOption(user, "Scenario", "Basic Range");
    await selectOption(user, "Agent", "kali-agent (Kali Linux)");

    mockApi.mockImplementation((path: string) => {
      if (path === "/mission-control/range/launch/") {
        return Promise.reject(new ApiError(409, { code: "conflict", message: "You already have an active range." }));
      }
      if (path === "/mission-control/launch-workspaces/") return Promise.resolve(LAUNCH_WORKSPACES);
      if (path === "/mission-control/scenarios/") return Promise.resolve(SCENARIOS);
      if (path === "/mission-control/agents/") return Promise.resolve(AGENTS);
      return Promise.reject(new Error("unexpected"));
    });

    await user.click(screen.getByRole("button", { name: "Launch range" }));

    expect(await screen.findByText("You already have an active range.")).toBeInTheDocument();
    const launchCalls = mockApi.mock.calls.filter(([path]) => path === "/mission-control/range/launch/");
    expect(launchCalls).toHaveLength(1);
    expect(navigateMock).not.toHaveBeenCalled();
  });

  it("disables the submit button and shows a busy label while launching", async () => {
    mockOptions();
    const user = userEvent.setup();
    renderRoute(<RangeLaunchPage />);

    await selectOption(user, "Scenario", "Basic Range");
    await selectOption(user, "Agent", "kali-agent (Kali Linux)");

    let resolveLaunch: (value: unknown) => void = () => {};
    mockApi.mockImplementation((path: string) => {
      if (path === "/mission-control/range/launch/") {
        return new Promise((resolve) => {
          resolveLaunch = resolve;
        });
      }
      if (path === "/mission-control/launch-workspaces/") return Promise.resolve(LAUNCH_WORKSPACES);
      if (path === "/mission-control/scenarios/") return Promise.resolve(SCENARIOS);
      if (path === "/mission-control/agents/") return Promise.resolve(AGENTS);
      return Promise.reject(new Error("unexpected"));
    });

    await user.click(screen.getByRole("button", { name: "Launch range" }));

    const busyButton = await screen.findByRole("button", { name: "Launching…" });
    expect(busyButton).toBeDisabled();
    resolveLaunch({ success: true, range: {} });
  });

  it("has no axe violations once options are loaded", async () => {
    mockOptions();
    const { container } = renderRoute(<RangeLaunchPage />);
    await screen.findByRole("button", { name: "Launch range" });
    const results = await axe(container);
    expect(results.violations).toEqual([]);
  });
});
