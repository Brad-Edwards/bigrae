// Live Django-rendered pages under the enforced Content Security Policy
// (ADR-036, #2090). The SPA journeys cover the React shell; these pages are the
// remaining server-rendered HTML that a URL still reaches. Each page must load
// with the enforced header and without a single CSP violation (the guard in
// support/csp.ts fails the test on any).
//
// Not covered here: `/login/` redirects to the external identity provider, and
// `/ctf/change-password/` needs an event participant account. Both stay with
// the deployed browser tier (#2117). The change-password template loads the
// same external script as `/ctf/login/` and has no inline code.

import { ACTORS, type ActorName } from "../support/actors";
import { test, expect } from "../support/csp";

interface DjangoPage {
  readonly path: string;
  readonly actor?: ActorName;
}

const PAGES: readonly DjangoPage[] = [
  { path: "/privacy/" },
  { path: "/ctf/login/" },
  { path: "/workspace-invitations/accept/" },
  { path: "/account/password/reset/done/" },
  // An invalid reset link renders the bounded invalid-link page.
  { path: "/account/password/reset/MQ/invalid-token/" },
  { path: "/admin/", actor: "staff" },
];

for (const { path, actor } of PAGES) {
  test(`${path}${actor ? ` as ${actor}` : ""} renders under the enforced policy`, async ({ newGuardedContext }) => {
    const context = await newGuardedContext({
      storageState: actor ? ACTORS[actor].state : { cookies: [], origins: [] },
    });
    const page = await context.newPage();
    const response = await page.goto(path);
    expect(response?.status()).toBeLessThan(400);
    expect(response?.headers()["content-security-policy"]).toContain("default-src 'none'");
    await expect(page.locator("body")).not.toBeEmpty();
    await page.waitForLoadState("load");
  });
}
