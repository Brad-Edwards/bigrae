import { afterEach, expect, it, vi } from "vitest";
import { exportCtfChallenges } from "./ctfAdminOps";

afterEach(() => vi.unstubAllGlobals());

it.each(["bigrae", "shifter", "ctfd"] as const)("exports challenges in the %s format", async (format) => {
  const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify({ challenges: [] }), { status: 200 }));
  vi.stubGlobal("fetch", fetch);

  await exportCtfChallenges("event-1", format);

  expect(fetch).toHaveBeenCalledWith(
    `/api/v1/ctf/events/event-1/challenges/export/?fmt=${format}`,
    expect.objectContaining({ method: "GET", credentials: "same-origin" }),
  );
});
