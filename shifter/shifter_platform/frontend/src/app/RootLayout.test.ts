import { describe, expect, it } from "vitest";

import { hasSmuggledSeparator } from "./RootLayout";

describe("hasSmuggledSeparator", () => {
  it.each([
    "/ctf/admin/events/..%2F..%2Fparticipants%2F7%2Fban%2F%3F/challenges/create",
    "/ctf/admin/events/x%2f..%2fy/challenges/create",
    "/scenario-editor/x%5C..%5Cy",
    "/mission-control/ranges/abc%23frag",
    "/mission-control/ranges/abc%252F..%252Fother",
  ])("refuses an encoded separator inside a segment: %s", (pathname) => {
    expect(hasSmuggledSeparator(pathname)).toBe(true);
  });

  it.each([
    "/",
    "/ctf/admin/events/0b6c4c7e-3c1e-4f9b-9d1d-2b8f0c7f1a10/challenges/create",
    "/mission-control/terminal/42",
    "/administer/organization/acme/workspaces/7/membership",
  ])("allows an ordinary SPA path: %s", (pathname) => {
    expect(hasSmuggledSeparator(pathname)).toBe(false);
  });
});
