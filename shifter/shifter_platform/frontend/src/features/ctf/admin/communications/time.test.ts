import { describe, expect, it } from "vitest";
import { formatInZone, zonedWallTimeToUtc } from "./time";

describe("zonedWallTimeToUtc", () => {
  it("converts a wall time in an IANA zone to one UTC instant", () => {
    expect(zonedWallTimeToUtc("2026-07-01", "12:00", "America/New_York")).toEqual({
      ok: true,
      utc: new Date("2026-07-01T16:00:00Z"),
    });
    expect(zonedWallTimeToUtc("2026-12-01", "09:30", "UTC")).toEqual({ ok: true, utc: new Date("2026-12-01T09:30:00Z") });
  });

  it("refuses a wall time that does not exist in the zone", () => {
    expect(zonedWallTimeToUtc("2026-03-08", "02:30", "America/New_York")).toEqual({ ok: false, reason: "nonexistent" });
  });

  it("refuses a wall time that occurs twice in the zone", () => {
    expect(zonedWallTimeToUtc("2026-11-01", "01:30", "America/New_York")).toEqual({ ok: false, reason: "ambiguous" });
  });

  it("refuses malformed input and unknown zones", () => {
    expect(zonedWallTimeToUtc("2026-13-01", "10:00", "UTC")).toEqual({ ok: false, reason: "invalid" });
    expect(zonedWallTimeToUtc("2026-01-01", "10:00", "Not/AZone")).toEqual({ ok: false, reason: "invalid" });
  });
});

it("formats an instant with its zone name", () => {
  expect(formatInZone(new Date("2026-07-01T16:00:00Z"), "America/New_York")).toContain("12:00");
  expect(formatInZone(new Date("2026-07-01T16:00:00Z"), "America/New_York")).toMatch(/EDT|GMT-4/);
});
