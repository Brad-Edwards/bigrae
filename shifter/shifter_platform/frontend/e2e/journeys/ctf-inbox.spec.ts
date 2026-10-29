// Participant inbox under the enforced CSP (#2053, ADR-051-R13, ADR-036).
//
// `seed_e2e_ctf` releases one safe message through the real admission path and
// one stored hostile row written below the admission validator. The guarded
// context fails the journey on any CSP violation, so hostile markup must render
// as inert text rather than create an element the policy then blocks.

import { test, expect, type Page } from "../support/csp";

import { ACTORS } from "../support/actors";

const SAFE = "Welcome to the E2E event";
const HOSTILE = "Stored hostile content";

async function xssFlag(page: Page): Promise<unknown> {
  return page.evaluate(() => (globalThis as unknown as Record<string, unknown>).__inboxXss);
}

test("a participant reads, acknowledges and views stored hostile content inertly", async ({ newGuardedContext }) => {
  const context = await newGuardedContext({ storageState: ACTORS.inbox.state });
  const page = await context.newPage();
  await page.goto("/ctf/inbox/");
  await expect(page.getByRole("heading", { name: "Inbox", exact: true })).toBeVisible();

  await page.getByRole("button", { name: new RegExp(SAFE) }).click();
  const safe = page.getByRole("region", { name: SAFE });
  await expect(safe.getByRole("link", { name: "help page" })).toHaveAttribute("href", "/ctf/help/");
  const acknowledge = safe.getByRole("button", { name: "Acknowledge" });
  if (await acknowledge.isVisible()) await acknowledge.click();
  await expect(safe.getByText(/^Acknowledged /)).toBeVisible();

  await page.getByRole("button", { name: new RegExp(HOSTILE) }).click();
  const hostile = page.getByRole("region", { name: HOSTILE });
  await expect(hostile).toContainText("Stored hostile content.");
  await expect(hostile.locator("img, script, iframe, a")).toHaveCount(0);
  expect(await xssFlag(page)).toBeUndefined();

  await context.close();
});

test("query input in the inbox URL is never rendered or executed", async ({ newGuardedContext }) => {
  const context = await newGuardedContext({ storageState: ACTORS.inbox.state });
  const page = await context.newPage();
  const probe = '<img src=x onerror="window.__inboxXss=1">';
  await page.goto(`/ctf/inbox/?view=${encodeURIComponent(probe)}`);

  const views = page.getByRole("navigation", { name: "Inbox views" });
  await expect(views.getByRole("link", { name: "Inbox", exact: true })).toHaveAttribute("aria-current", "page");
  await expect(page.getByRole("button", { name: new RegExp(SAFE) })).toBeVisible();
  expect(await page.content()).not.toContain("onerror=");
  expect(await xssFlag(page)).toBeUndefined();

  await context.close();
});
