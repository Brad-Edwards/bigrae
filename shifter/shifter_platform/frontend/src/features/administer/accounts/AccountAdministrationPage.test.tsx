import { screen, waitFor } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";

import { ApiError } from "@/api/errors";
import { renderRoute, setupUser } from "@/test/utils";

vi.mock("@/api/client", () => ({ apiFetch: vi.fn() }));
import { apiFetch } from "@/api/client";

import { AccountAdministrationPage } from "./AccountAdministrationPage";

const mockApi = vi.mocked(apiFetch);
const account = { uuid: "11111111-1111-1111-1111-111111111111", kind: "individual", name: "Personal" };

beforeEach(() => {
  mockApi.mockReset();
  mockApi.mockImplementation((path, options) => {
    if (path === "/accounts/current/") return Promise.resolve(account);
    if (path === `/accounts/${account.uuid}/` && options?.method === "PATCH") return Promise.resolve({ ...account, name: "Renamed" });
    if (path.endsWith("/memberships/")) return Promise.resolve({ count: 0, results: [] });
    return Promise.resolve({ count: 0, results: [] });
  });
});

it("loads an individual account without any account or subdivision selector", async () => {
  renderRoute(<AccountAdministrationPage individual />);
  expect(await screen.findByRole("heading", { name: "Personal" })).toBeInTheDocument();
  expect(screen.queryByRole("combobox", { name: /organization|workspace|account/i })).not.toBeInTheDocument();
  expect(mockApi.mock.calls.some(([path]) => path.includes("/organizations/"))).toBe(false);
  expect(mockApi.mock.calls.some(([path]) => path === "/accounts/")).toBe(false);
});

it("submits only the account name to the exact selected account", async () => {
  const user = setupUser();
  renderRoute(<AccountAdministrationPage individual />);
  const name = await screen.findByLabelText("Account name");
  await user.clear(name);
  await user.type(name, "Renamed");
  await user.click(screen.getByRole("button", { name: "Save account" }));
  await waitFor(() => expect(mockApi).toHaveBeenCalledWith(`/accounts/${account.uuid}/`, expect.objectContaining({ method: "PATCH", body: { name: "Renamed" } })));
});

it("keeps denied account data out of the page", async () => {
  mockApi.mockRejectedValue(new ApiError(403, { code: "account_access_denied", message: "Account access denied" }));
  renderRoute(<AccountAdministrationPage individual />);
  expect(await screen.findByRole("alert")).toHaveTextContent("Account access denied");
  expect(screen.queryByLabelText("Account name")).not.toBeInTheDocument();
});

it("pages business accounts using the authorized server count", async () => {
  mockApi.mockImplementation((path, options) => {
    if (path === "/accounts/") return Promise.resolve({ count: 51, results: options?.query?.offset ? [] : [{ ...account, kind: "team", name: "Team" }] });
    return Promise.resolve({ count: 0, results: [] });
  });
  const user = setupUser();
  renderRoute(<AccountAdministrationPage />);
  expect(await screen.findByRole("button", { name: "Team" })).toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "Next accounts" }));
  await waitFor(() => expect(mockApi).toHaveBeenCalledWith("/accounts/", expect.objectContaining({ query: expect.objectContaining({ offset: 50 }) })));
});

it("selects the individual presentation from the admitted principal context", async () => {
  mockApi.mockImplementation((path) => {
    if (path === "/accounts/context/") return Promise.resolve({ principal_kind: "human", individual_account: account, accounts: { count: 1, results: [account] }, installation_actions: [] });
    if (path === "/accounts/current/") return Promise.resolve(account);
    return Promise.resolve({ count: 0, results: [] });
  });
  const { AccountAdministrationEntry } = await import("./AccountAdministrationPage");
  renderRoute(<AccountAdministrationEntry />);
  expect(await screen.findByRole("heading", { name: "Personal" })).toBeInTheDocument();
  expect(screen.queryByLabelText("Search accounts")).not.toBeInTheDocument();
});

it("binds subdivision editing to the selected business account with a narrow PATCH", async () => {
  const team = { ...account, kind: "team", name: "Team" };
  const org = { uuid: "org-one", name: "Division", is_default: true };
  mockApi.mockImplementation(path => {
    if (path === "/accounts/") return Promise.resolve({ count: 1, results: [team] });
    if (path === `/accounts/${account.uuid}/`) return Promise.resolve(team);
    if (path.endsWith("/organizations/")) return Promise.resolve({ count: 1, results: [org] });
    if (path.endsWith("/organizations/org-one/")) return Promise.resolve({ ...org, description: "Original", support_email: "help@example.test", support_url: "https://example.test" });
    return Promise.resolve({ count: 0, results: [] });
  });
  const user = setupUser();
  renderRoute(<AccountAdministrationPage />);
  await user.click(await screen.findByRole("button", { name: "Team" }));
  await user.click(screen.getByRole("tab", { name: "Organizations" }));
  await user.click(await screen.findByRole("button", { name: "Division (default)" }));
  await user.clear(await screen.findByLabelText("Description"));
  await user.type(screen.getByLabelText("Description"), "Changed");
  await user.click(screen.getByRole("button", { name: "Save organization" }));
  await waitFor(() => expect(mockApi).toHaveBeenCalledWith(`/accounts/${account.uuid}/organizations/org-one/`, expect.objectContaining({ method: "PATCH", body: { description: "Changed" } })));
});

it("rechecks selected account details instead of displaying a stale collection row", async () => {
  mockApi.mockImplementation(path => {
    if (path === "/accounts/") return Promise.resolve({ count: 1, results: [{ ...account, kind: "team", name: "Previously visible" }] });
    if (path === `/accounts/${account.uuid}/`) return Promise.reject(new ApiError(403, { code: "denied", message: "Selected account denied" }));
    return Promise.resolve({ count: 0, results: [] });
  });
  const user = setupUser(); renderRoute(<AccountAdministrationPage />);
  await user.click(await screen.findByRole("button", { name: "Previously visible" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("Selected account denied");
  expect(screen.queryByLabelText("Account name")).not.toBeInTheDocument();
});

it("lets a personal-account owner reach another authorized business account", async () => {
  mockApi.mockImplementation(path => {
    if (path === "/accounts/context/") return Promise.resolve({ principal_uuid: "human", individual_account: account, accounts: { count: 2, results: [account, { ...account, uuid: "business", kind: "team", name: "Business" }] }, installation_actions: [] });
    if (path === "/accounts/current/") return Promise.resolve(account);
    if (path === "/accounts/") return Promise.resolve({ count: 2, results: [account, { ...account, uuid: "business", kind: "team", name: "Business" }] });
    return Promise.resolve({ count: 0, results: [] });
  });
  const { AccountAdministrationEntry } = await import("./AccountAdministrationPage");
  const user = setupUser(); renderRoute(<AccountAdministrationEntry />);
  await screen.findByRole("heading", { name: "Personal" });
  await user.click(screen.getByRole("button", { name: "Other accounts" }));
  expect(await screen.findByRole("button", { name: "Business" })).toBeInTheDocument();
});

it.each(["organization", "workspace"])("opens a directly granted %s without selecting its parent account", async kind => {
  const scope = { kind, uuid: "descendant", name: "Directly granted scope", account_uuid: "parent", organization_uuid: "division", is_default: false };
  mockApi.mockImplementation((path, options) => {
    if (path === "/accounts/context/") return Promise.resolve({ principal_uuid: "principal", individual_account: null, accounts: { count: 0, results: [] }, installation_actions: [], has_descendant_scopes: true });
    if (path === "/administer/scopes/") return Promise.resolve({ count: options?.query?.kind === kind ? 1 : 0, results: options?.query?.kind === kind ? [scope] : [] });
    if (path === "/accounts/parent/organizations/descendant/") return Promise.resolve({ name: scope.name, description: "", support_email: "", support_url: "" });
    if (path === "/accounts/parent/organizations/division/workspaces/descendant/") return Promise.resolve({ ...scope, archived_at: null, owner_principal_uuids: [], egress_policy: "none" });
    return Promise.resolve({ count: 0, results: [] });
  });
  const { AccountAdministrationEntry } = await import("./AccountAdministrationPage");
  const user = setupUser(); renderRoute(<AccountAdministrationEntry />);
  await user.selectOptions(await screen.findByLabelText("Scope type"), kind);
  await user.click(await screen.findByRole("button", { name: scope.name }));
  const expected = kind === "organization" ? "/accounts/parent/organizations/descendant/" : "/accounts/parent/organizations/division/workspaces/descendant/";
  await waitFor(() => expect(mockApi).toHaveBeenCalledWith(expected, expect.anything()));
  expect(mockApi.mock.calls.some(([path]) => path === "/accounts/parent/")).toBe(false);
});

it("retains a pending creation receipt and retries the same declaration", async () => {
  mockApi.mockImplementation((path, options) => {
    if (path === "/accounts/" && options?.method === "POST") {
      return Promise.resolve(options.body && typeof options.body === "object" && "publication_receipt" in options.body
        ? { ...account, kind: "team" } : { uuid: account.uuid, publication_receipt: "receipt-one", publication_state: "pending" });
    }
    return Promise.resolve({ count: 0, results: [] });
  });
  const user = setupUser(); renderRoute(<AccountAdministrationPage />);
  await user.type(screen.getByLabelText("New account name"), "New team");
  await user.click(screen.getByRole("button", { name: "Create account" }));
  await user.click(await screen.findByRole("button", { name: "Retry access setup" }));
  await waitFor(() => expect(mockApi).toHaveBeenCalledWith("/accounts/", expect.objectContaining({ method: "POST", body: { kind: "team", name: "New team", publication_receipt: "receipt-one" } })));
  await waitFor(() => expect(screen.queryByRole("button", { name: "Retry access setup" })).not.toBeInTheDocument());
});


it("keeps the receipt available when a publication retry fails", async () => {
  let posts = 0;
  mockApi.mockImplementation((path, options) => {
    if (path === "/accounts/" && options?.method === "POST") {
      posts += 1;
      if (posts === 1) return Promise.resolve({ uuid: account.uuid, publication_receipt: "retained", publication_state: "pending" });
      if (posts === 2) return Promise.reject(new ApiError(503, { code: "unavailable", message: "Try again" }));
      return Promise.resolve({ ...account, kind: "team" });
    }
    return Promise.resolve({ count: 0, results: [] });
  });
  const user = setupUser(); renderRoute(<AccountAdministrationPage />);
  await user.type(screen.getByLabelText("New account name"), "New team");
  await user.click(screen.getByRole("button", { name: "Create account" }));
  await user.click(await screen.findByRole("button", { name: "Retry access setup" }));
  await screen.findByText("Try again");
  expect(screen.getByRole("button", { name: "Create account" })).toBeDisabled();
  await user.click(screen.getByRole("button", { name: "Retry access setup" }));
  await waitFor(() => expect(posts).toBe(3));
  const bodies = mockApi.mock.calls.filter(([, options]) => options?.method === "POST").map(([, options]) => options?.body);
  expect(bodies[1]).toEqual({ name: "New team", kind: "team", publication_receipt: "retained" });
  expect(bodies[2]).toEqual(bodies[1]);
});

function directlyGrantedOrganizations() {
  const organizations = ["Alpha", "Beta"].map((name, index) => ({
    kind: "organization", uuid: `org-${index}`, name,
    account_uuid: "parent", organization_uuid: `org-${index}`, is_default: false,
  }));
  mockApi.mockImplementation((path, options) => {
    if (path === "/accounts/context/") return Promise.resolve({ principal_uuid: "principal", individual_account: null, accounts: { count: 0, results: [] }, installation_actions: [], has_descendant_scopes: true });
    if (path === "/administer/scopes/") return Promise.resolve({ count: 2, results: organizations });
    if (path.endsWith("/workspaces/") && options?.method === "POST") return Promise.resolve({ uuid: "accepted-workspace", publication_receipt: "alpha-receipt", publication_state: "pending" });
    if (path.endsWith("/memberships/")) return Promise.resolve({ count: 1, results: [{ principal_uuid: "alpha-member" }] });
    if (path.endsWith("/org-0/") || path.endsWith("/org-1/")) return Promise.resolve({ name: "Division", description: "", support_email: "", support_url: "" });
    return Promise.resolve({ count: 0, results: [] });
  });
}

it("binds pending workspace retries to their original organization and clears them on selection change", async () => {
  directlyGrantedOrganizations();
  const { AccountAdministrationEntry } = await import("./AccountAdministrationPage");
  const user = setupUser(); renderRoute(<AccountAdministrationEntry />);
  await user.click(await screen.findByRole("button", { name: "Alpha" }));
  await user.type(await screen.findByLabelText("New workspace name"), "Alpha workspace");
  await user.click(screen.getByRole("button", { name: "Create workspace" }));
  await user.click(await screen.findByRole("button", { name: "Retry access setup" }));
  await waitFor(() => expect(mockApi).toHaveBeenCalledWith("/accounts/parent/organizations/org-0/workspaces/", expect.objectContaining({ method: "POST", body: { name: "Alpha workspace", publication_receipt: "alpha-receipt" } })));
  await user.click(screen.getByRole("button", { name: "Beta" }));
  expect(screen.queryByRole("button", { name: "Retry access setup" })).not.toBeInTheDocument();
  expect(screen.getByLabelText("New workspace name")).toHaveValue("");
  await user.type(screen.getByLabelText("New workspace name"), "Beta workspace");
  await user.click(screen.getByRole("button", { name: "Create workspace" }));
  await waitFor(() => expect(mockApi).toHaveBeenCalledWith("/accounts/parent/organizations/org-1/workspaces/", expect.objectContaining({ method: "POST", body: { name: "Beta workspace" } })));
  expect(mockApi.mock.calls.filter(([path, options]) => path.includes("org-1") && options?.method === "POST").every(([, options]) => !Object.hasOwn(options?.body ?? {}, "publication_receipt"))).toBe(true);
});

it("does not carry a membership removal confirmation to another directly granted organization", async () => {
  directlyGrantedOrganizations();
  const { AccountAdministrationEntry } = await import("./AccountAdministrationPage");
  const user = setupUser(); renderRoute(<AccountAdministrationEntry />);
  await user.click(await screen.findByRole("button", { name: "Alpha" }));
  await user.click(screen.getByRole("tab", { name: "Members" }));
  await user.click(await screen.findByRole("button", { name: "alpha-member" }));
  expect(screen.getByRole("button", { name: "Confirm removal" })).toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "Beta" }));
  await user.click(screen.getByRole("tab", { name: "Members" }));
  expect(screen.queryByRole("button", { name: "Confirm removal" })).not.toBeInTheDocument();
  expect(mockApi.mock.calls.some(([, options]) => options?.method === "DELETE")).toBe(false);
});
