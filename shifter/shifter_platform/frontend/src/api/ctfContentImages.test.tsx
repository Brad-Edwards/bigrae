import type { ReactNode } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

vi.mock("@/api/client", () => ({ apiFetch: vi.fn().mockResolvedValue({}) }));

import { contentImagesRefetchInterval, ctfKeys, useSubmitFlag, useUseHint } from "./ctf";
import { eventContentImagesRefetchInterval } from "./ctfAdmin";

describe("image map refresh while copies are pending (#2090)", () => {
  it("polls the participant map only while visible copies are pending", () => {
    expect(contentImagesRefetchInterval({ images: [], pending: 2 })).toBe(30_000);
    expect(contentImagesRefetchInterval({ images: [], pending: 0 })).toBe(false);
    expect(contentImagesRefetchInterval(undefined)).toBe(false);
  });

  it("polls the organizer map only while any copy is pending", () => {
    const row = (status: "pending" | "ready" | "failed") => ({
      source_url: "https://img.example.test/a.png",
      status,
      failure_code: "",
      url: null,
    });
    expect(eventContentImagesRefetchInterval({ images: [row("ready"), row("pending")] })).toBe(30_000);
    expect(eventContentImagesRefetchInterval({ images: [row("ready"), row("failed")] })).toBe(false);
    expect(eventContentImagesRefetchInterval(undefined)).toBe(false);
  });
});

describe("participant map follows read visibility (#2090)", () => {
  function wrapper(client: QueryClient) {
    function Wrapper({ children }: Readonly<{ children: ReactNode }>) {
      return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
    }
    return Wrapper;
  }

  it("refreshes after a flag submission", async () => {
    const client = new QueryClient();
    const invalidate = vi.spyOn(client, "invalidateQueries");
    const { result } = renderHook(() => useSubmitFlag("c1"), { wrapper: wrapper(client) });

    await act(async () => {
      await result.current.mutateAsync("FLAG{x}");
    });

    await waitFor(() => expect(invalidate).toHaveBeenCalledWith({ queryKey: ctfKeys.contentImages() }));
  });

  it("refreshes after a hint unlock", async () => {
    const client = new QueryClient();
    const invalidate = vi.spyOn(client, "invalidateQueries");
    const { result } = renderHook(() => useUseHint("c1"), { wrapper: wrapper(client) });

    await act(async () => {
      await result.current.mutateAsync(undefined);
    });

    await waitFor(() => expect(invalidate).toHaveBeenCalledWith({ queryKey: ctfKeys.contentImages() }));
  });
});
