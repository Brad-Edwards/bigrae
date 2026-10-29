import { beforeEach, describe, expect, it, vi } from "vitest";
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { axe } from "vitest-axe";

import { ApiError } from "@/api/errors";
import { renderRoute } from "@/test/utils";

vi.mock("@/api/client", () => ({ apiFetch: vi.fn() }));

import { apiFetch } from "@/api/client";

import { ScenarioListPage } from "./ScenarioListPage";

const mockApi = vi.mocked(apiFetch);

const sharedCatalog = { kind: "installation", name: "Shared catalog", scope_kind: "installation", account: null, organization: null, workspace: null };

function catalogResult(result: unknown[]) {
  mockApi.mockImplementation((path) => Promise.resolve(path === "/cms/content-scopes/"
    ? { count: 1, results: [sharedCatalog] } : result));
}

function entry(overrides: Record<string, unknown> = {}) {
  return {
    id: "example",
    name: "example",
    scenario_type: "raes",
    source: "raes",
    is_default: false,
    enabled: true,
    staff_only: false,
    launchable: true,
    raes: {
      source_kind: "repo",
      contract_kind: "raes",
      contract_profile: "shifter",
      package_ref: "scenario-dev/example",
      package_version: "1.0.0",
      package_digest: "sha256:abc",
      lock_ref: "",
      lock_digest: "",
      conformance_status: "passed",
      conformance_report_ref: "",
      provenance_summary: {},
    },
    ...overrides,
  };
}

beforeEach(() => {
  mockApi.mockReset();
});

describe("ScenarioListPage", () => {
  it("renders only the RAES catalog contract", async () => {
    catalogResult([entry()]);
    renderRoute(<ScenarioListPage />);
    expect(await screen.findByRole("link", { name: "example" })).toBeInTheDocument();
    const table = screen.getByRole("table");
    expect(within(table).getByText("RAES")).toBeInTheDocument();
    expect(within(table).getByText("Yes")).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "New scenario" })).not.toBeInTheDocument();
  });

  it("shows the RAES registration empty state", async () => {
    catalogResult([]);
    renderRoute(<ScenarioListPage />);
    expect(await screen.findByText("No scenarios yet")).toBeInTheDocument();
    expect(screen.getByText("Register a RAES pack to populate the catalog.")).toBeInTheDocument();
  });

  it("renders an error state on failure", async () => {
    mockApi.mockImplementation((path) => path === "/cms/content-scopes/"
      ? Promise.resolve({ count: 1, results: [sharedCatalog] })
      : Promise.reject(new ApiError(500, { code: "error", message: "boom" })));
    renderRoute(<ScenarioListPage />);
    expect(await screen.findByText("Could not load scenarios")).toBeInTheDocument();
  });

  it("filters by name or id and reports an empty filtered result", async () => {
    const user = userEvent.setup();
    catalogResult([
      entry({ id: "alpha-id", name: "First Exercise" }),
      entry({ id: "beta-id", name: "Second Exercise" }),
    ]);
    renderRoute(<ScenarioListPage />);
    await screen.findByRole("link", { name: "First Exercise" });
    const search = screen.getByRole("textbox", { name: "Search scenarios" });
    await user.type(search, " FIRST ");
    expect(screen.getByRole("link", { name: "First Exercise" })).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Second Exercise" })).not.toBeInTheDocument();
    await user.clear(search);
    await user.type(search, "beta-id");
    expect(screen.getByRole("link", { name: "Second Exercise" })).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "First Exercise" })).not.toBeInTheDocument();
    await user.type(search, "-absent");
    expect(screen.getByText("No scenarios match these filters")).toBeInTheDocument();
    await user.clear(search);
    expect(screen.getAllByRole("link")).toHaveLength(2);
  });

  it("selects source and availability filters and restores all rows", async () => {
    const user = userEvent.setup();
    catalogResult([
      entry({ id: "enabled", name: "Enabled exercise" }),
      entry({ id: "disabled", name: "Disabled exercise", enabled: false }),
    ]);
    renderRoute(<ScenarioListPage />);
    await screen.findByRole("link", { name: "Enabled exercise" });
    await user.click(screen.getByRole("combobox", { name: "Filter by source" }));
    await user.click(screen.getByRole("option", { name: "Raes" }));
    expect(screen.getAllByRole("link")).toHaveLength(2);
    await user.click(screen.getByRole("combobox", { name: "Filter by availability" }));
    await user.click(screen.getByRole("option", { name: "Enabled" }));
    expect(screen.getByRole("link", { name: "Enabled exercise" })).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Disabled exercise" })).not.toBeInTheDocument();
    await user.click(screen.getByRole("combobox", { name: "Filter by availability" }));
    await user.click(screen.getByRole("option", { name: "Disabled" }));
    expect(screen.getByRole("link", { name: "Disabled exercise" })).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Enabled exercise" })).not.toBeInTheDocument();
    await user.click(screen.getByRole("combobox", { name: "Filter by availability" }));
    await user.click(screen.getByRole("option", { name: "All availability" }));
    await user.click(screen.getByRole("combobox", { name: "Filter by source" }));
    await user.click(screen.getByRole("option", { name: "All sources" }));
    expect(screen.getAllByRole("link")).toHaveLength(2);
  });

  it("has no axe violations when loaded", async () => {
    catalogResult([entry()]);
    const { container } = renderRoute(<ScenarioListPage />);
    await screen.findByRole("link", { name: "example" });
    const results = await axe(container);
    expect(results.violations).toEqual([]);
  });

  it("requests the selected tenant ancestry and keeps catalogs in separate cache entries", async () => {
    const user = userEvent.setup();
    const organization = { kind: "organization", name: "Design team", scope_kind: "account", account: "account-a", organization: "org-a", workspace: null };
    const workspace = { ...organization, kind: "workspace", name: "Exercise authors", workspace: "workspace-a" };
    mockApi.mockImplementation((path, options) => Promise.resolve(path === "/cms/content-scopes/"
      ? { count: 2, results: [organization, workspace] }
      : [entry({ id: options?.query?.workspace ? "workspace-pack" : "organization-pack" })]));
    renderRoute(<ScenarioListPage />);
    await screen.findByText("organization-pack", { selector: "span" });
    expect(mockApi).toHaveBeenCalledWith("/cms/catalog/", expect.objectContaining({ query: { scope_kind: "account", account: "account-a", organization: "org-a", workspace: null } }));
    await user.selectOptions(screen.getByLabelText("Catalog"), JSON.stringify(["account", "account-a", "org-a", "workspace-a"]));
    await screen.findByText("workspace-pack", { selector: "span" });
    expect(screen.queryByText("organization-pack", { selector: "span" })).not.toBeInTheDocument();
    expect(mockApi).toHaveBeenCalledWith("/cms/catalog/", expect.objectContaining({ query: { scope_kind: "account", account: "account-a", organization: "org-a", workspace: "workspace-a" } }));
    await user.selectOptions(screen.getByLabelText("Catalog"), JSON.stringify(["account", "account-a", "org-a", null]));
    await screen.findByText("organization-pack", { selector: "span" });
    expect(screen.queryByText("workspace-pack", { selector: "span" })).not.toBeInTheDocument();
  });

  it("pages authorized catalogs and retains the selected catalog while browsing", async () => {
    const user = userEvent.setup();
    const later = { kind: "workspace", name: "Later catalog", scope_kind: "account", account: "account-b", organization: "org-b", workspace: "workspace-b" };
    mockApi.mockImplementation((path, options) => Promise.resolve(path === "/cms/content-scopes/"
      ? { count: 51, results: options?.query?.offset ? [later] : [sharedCatalog] }
      : [entry({ id: options?.query?.workspace ? "later-pack" : "shared-pack" })]));
    renderRoute(<ScenarioListPage />);
    await screen.findByText("shared-pack", { selector: "span" });
    await user.click(screen.getByRole("button", { name: "Next catalogs" }));
    await screen.findByRole("option", { name: "Workspace: Later catalog" });
    expect(screen.getByText("shared-pack", { selector: "span" })).toBeInTheDocument();
    expect(mockApi).toHaveBeenCalledWith("/cms/content-scopes/", expect.objectContaining({ query: { offset: 50, limit: 50 } }));
    await user.selectOptions(screen.getByLabelText("Catalog"), JSON.stringify(["account", "account-b", "org-b", "workspace-b"]));
    await screen.findByText("later-pack", { selector: "span" });
  });

  it("does not substitute a global catalog when no content scope is authorized", async () => {
    mockApi.mockResolvedValue({ count: 0, results: [] });
    renderRoute(<ScenarioListPage />);
    await screen.findByText("No scenario catalogs are available.");
    expect(mockApi.mock.calls.every(([path]) => path === "/cms/content-scopes/")).toBe(true);
  });

  it("reports unavailable catalog discovery without requesting an arbitrary scope", async () => {
    mockApi.mockRejectedValue(new ApiError(503, { code: "unavailable", message: "Unavailable" }));
    renderRoute(<ScenarioListPage />);
    await waitFor(() => expect(screen.getByText("Could not load catalogs")).toBeInTheDocument());
    expect(mockApi.mock.calls.every(([path]) => path === "/cms/content-scopes/")).toBe(true);
  });
});
