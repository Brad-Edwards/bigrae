import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { ContentImageProvider, resolveContentImage, useContentImageMap } from "./contentImages";

const map = new Map([["https://img.example.test/a.png", "/api/v1/ctf/content-images/1/"]]);

describe("resolveContentImage", () => {
  it("uses the first-party copy of a mapped external image", () => {
    expect(resolveContentImage(map, "https://img.example.test/a.png")).toEqual({
      kind: "image",
      src: "/api/v1/ctf/content-images/1/",
    });
  });

  it("links to an external image that has no ready copy", () => {
    expect(resolveContentImage(map, "https://img.example.test/b.png")).toEqual({
      kind: "link",
      href: "https://img.example.test/b.png",
    });
    expect(resolveContentImage(map, "http://img.example.test/b.png")).toEqual({
      kind: "link",
      href: "http://img.example.test/b.png",
    });
  });

  it("keeps same-origin and relative images", () => {
    expect(resolveContentImage(map, "/static/images/logo.png")).toEqual({ kind: "image", src: "/static/images/logo.png" });
    expect(resolveContentImage(map, "images/logo.png")).toEqual({ kind: "image", src: "images/logo.png" });
  });

  it("drops empty, protocol-relative and other-scheme sources", () => {
    expect(resolveContentImage(map, "")).toBeNull();
    expect(resolveContentImage(map, undefined)).toBeNull();
    expect(resolveContentImage(map, "//img.example.test/a.png")).toBeNull();
    expect(resolveContentImage(map, "ftp://img.example.test/a.png")).toBeNull();
  });
});

function Probe() {
  const current = useContentImageMap();
  return <span>{[...current.entries()].map(([k, v]) => `${k}=${v}`).join(",") || "empty"}</span>;
}

describe("ContentImageProvider", () => {
  it("exposes only ready copies and defaults to an empty map", () => {
    const { rerender } = render(<Probe />);
    expect(screen.getByText("empty")).toBeInTheDocument();

    rerender(
      <ContentImageProvider
        images={[
          { source_url: "https://img.example.test/a.png", url: "/api/v1/ctf/content-images/1/" },
          { source_url: "https://img.example.test/b.png", url: null },
        ]}
      >
        <Probe />
      </ContentImageProvider>,
    );
    expect(screen.getByText("https://img.example.test/a.png=/api/v1/ctf/content-images/1/")).toBeInTheDocument();
  });
});
