// Native account-administration lifecycle through real policy and DRF services.
import { test, expect } from "@playwright/test";

import { ACTORS } from "../support/actors";

test.use({ storageState: ACTORS.staff.state });

test("create, rename, archive, and restore a workspace", async ({ page }) => {
  const name = `E2E Workspace ${Date.now()}`;
  const renamed = `${name} renamed`;
  await page.goto("/administer/accounts");
  await page.getByRole("region", { name: "Browser storage notice" }).getByRole("button", { name: "Dismiss" }).click();
  await page.getByRole("button", { name: "E2E Team", exact: true }).click();
  await page.getByRole("tab", { name: "Organizations", exact: true }).click();
  await page.getByRole("button", { name: /\(default\)$/ }).click();
  await page.getByLabel("New workspace name", { exact: true }).fill(name);
  await page.getByRole("button", { name: "Create workspace", exact: true }).click();
  await page.getByRole("button", { name, exact: true }).click();
  await expect(page.getByRole("heading", { name, exact: true })).toBeVisible();
  await page.getByLabel("Workspace name", { exact: true }).fill(renamed);
  await page.getByRole("button", { name: "Rename workspace", exact: true }).click();
  await expect(page.getByRole("heading", { name: renamed, exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Archive workspace", exact: true }).click();
  await page.getByRole("button", { name: "Confirm workspace change", exact: true }).click();
  await expect(page.getByRole("button", { name: "Restore workspace", exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Restore workspace", exact: true }).click();
  await page.getByRole("button", { name: "Confirm workspace change", exact: true }).click();
  await expect(page.getByRole("button", { name: "Archive workspace", exact: true })).toBeVisible();
});
