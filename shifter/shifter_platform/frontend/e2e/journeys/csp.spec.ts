// Enforced Content Security Policy at the browser boundary (ADR-036, #2090).
// The portal sends one enforced policy, the browser blocks injected inline
// script and off-origin connections, and same-origin WebSockets (terminal,
// channels) stay allowed under `connect-src 'self'`.
//
// These tests expect violations, so they record them on a dedicated context
// instead of the auto-failing guard the other journeys use.

import { test as base, expect, type Page } from "@playwright/test";

import { ACTORS } from "../support/actors";
import { guardCsp, type CspViolation } from "../support/csp";

const test = base.extend<{ guarded: { page: Page; violations: CspViolation[] } }>({
  guarded: async ({ browser }, use) => {
    const context = await browser.newContext({ storageState: ACTORS.standard.state });
    const violations = await guardCsp(context);
    const page = await context.newPage();
    await use({ page, violations });
    await context.close();
  },
});

test("the SPA document carries the enforced policy and no report-only policy", async ({ guarded }) => {
  const response = await guarded.page.goto("/mission-control");
  const headers = response?.headers() ?? {};
  expect(headers["content-security-policy"]).toContain("default-src 'none'");
  expect(headers["content-security-policy"]).toContain("script-src-attr 'none'");
  expect(headers["content-security-policy-report-only"]).toBeUndefined();
  expect(guarded.violations).toEqual([]);
});

test("injected inline script and handlers do not execute", async ({ guarded }) => {
  const { page, violations } = guarded;
  await page.goto("/mission-control");
  await expect(page.getByRole("main")).toBeVisible();

  const executed = await page.evaluate(async () => {
    const flags = window as unknown as Record<string, unknown>;
    const script = document.createElement("script");
    script.textContent = "window.__cspInlineRan = true;";
    document.body.append(script);

    const handler = document.createElement("img");
    handler.setAttribute("onerror", "window.__cspHandlerRan = true");
    handler.src = "data:,not-an-image";
    document.body.append(handler);
    await new Promise((resolve) => setTimeout(resolve, 250));
    return { inline: flags.__cspInlineRan === true, handler: flags.__cspHandlerRan === true };
  });

  expect(executed).toEqual({ inline: false, handler: false });
  const directives = violations.map((v) => v.directive);
  expect(directives).toContain("script-src-elem");
  expect(directives).toContain("script-src-attr");
});

test("script-created style elements apply and injected style markup is blocked", async ({ guarded }) => {
  const { page, violations } = guarded;
  await page.goto("/mission-control");
  await expect(page.getByRole("main")).toBeVisible();

  // The library path (xterm, dialog scroll lock): createElement("style").
  const libraryStyleApplied = await page.evaluate(() => {
    const probe = document.createElement("div");
    probe.id = "csp-style-probe";
    document.body.append(probe);
    const style = document.createElement("style");
    document.head.append(style);
    style.textContent = "#csp-style-probe { width: 7px; }";
    return getComputedStyle(probe).width;
  });
  expect(libraryStyleApplied).toBe("7px");
  expect(violations).toEqual([]);

  // Injected markup never passes through the nonce factory.
  const injectedStyleApplied = await page.evaluate(async () => {
    const probe = document.createElement("div");
    probe.id = "csp-injected-probe";
    document.body.append(probe);
    const host = document.createElement("div");
    host.innerHTML = "<style>#csp-injected-probe { width: 9px; }</style>";
    document.body.append(host);
    await new Promise((resolve) => setTimeout(resolve, 100));
    return getComputedStyle(probe).width;
  });
  expect(injectedStyleApplied).not.toBe("9px");
  await expect.poll(() => violations.map((v) => v.directive)).toContain("style-src-elem");
});

test("same-origin WebSockets are allowed and off-origin connections are blocked", async ({ guarded }) => {
  const { page, violations } = guarded;
  await page.goto("/mission-control");
  await expect(page.getByRole("main")).toBeVisible();

  await page.evaluate(async () => {
    const settle = (socket: WebSocket) =>
      new Promise<void>((resolve) => {
        socket.addEventListener("open", () => socket.close());
        socket.addEventListener("close", () => resolve());
        socket.addEventListener("error", () => resolve());
      });
    const sameOrigin = new WebSocket(`${location.origin.replace(/^http/, "ws")}/ws/csp-probe/`);
    await settle(sameOrigin);
  });
  expect(violations).toEqual([]);

  await page.evaluate(async () => {
    try {
      const offOrigin = new WebSocket("ws://csp-probe.invalid/");
      offOrigin.close();
    } catch {
      // Some engines throw synchronously; the violation event is the oracle.
    }
    try {
      await fetch("https://csp-probe.invalid/");
    } catch {
      // Blocked fetches reject; the violation event is the oracle.
    }
  });
  await expect.poll(() => violations.filter((v) => v.directive === "connect-src").length).toBe(2);
});
