import { describe, expect, it } from "vitest";
import { render } from "@testing-library/react";

import { ContentImageProvider } from "./contentImages";
import { MarkdownContent } from "./MarkdownContent";

/** The href the first rendered anchor carries, or "" when the element/attribute is absent. */
function anchorHref(container: HTMLElement): string {
  return container.querySelector("a")?.getAttribute("href") ?? "";
}

describe("MarkdownContent (safe render)", () => {
  it("does not inject raw HTML script tags", () => {
    const { container } = render(<MarkdownContent text={"Hi <script>alert(1)</script> there"} />);
    expect(container.querySelector("script")).toBeNull();
  });

  // The renderer neutralizes any non-allowlisted scheme to an empty href, so the
  // tests assert that exact safe outcome rather than a per-scheme prefix check.
  it("neutralizes a javascript: link to an empty href", () => {
    const { container } = render(<MarkdownContent text={"[click](javascript:alert(1))"} />);
    expect(anchorHref(container)).toBe("");
  });

  it("neutralizes a data: link to an empty href", () => {
    const { container } = render(<MarkdownContent text={"[x](data:text/html;base64,PHNjcmlwdD4=)"} />);
    expect(anchorHref(container)).toBe("");
  });

  it("neutralizes a vbscript: link to an empty href", () => {
    const { container } = render(<MarkdownContent text={"[x](vbscript:msgbox(1))"} />);
    expect(anchorHref(container)).toBe("");
  });

  it("keeps safe https links intact", () => {
    const { container } = render(<MarkdownContent text={"[docs](https://example.test/guide)"} />);
    expect(anchorHref(container)).toBe("https://example.test/guide");
  });

  it("renders the first-party copy of a mapped external image (#2090)", () => {
    const { container } = render(
      <ContentImageProvider images={[{ source_url: "https://example.test/net.png", url: "/api/v1/ctf/content-images/1/" }]}>
        <MarkdownContent text={"![diagram](https://example.test/net.png)"} />
      </ContentImageProvider>,
    );
    const img = container.querySelector("img");
    expect(img?.getAttribute("src")).toBe("/api/v1/ctf/content-images/1/");
    expect(img?.getAttribute("alt")).toBe("diagram");
  });

  it("links to an external image that has no ready copy instead of loading it", () => {
    const { container } = render(<MarkdownContent text={"![diagram](https://example.test/net.png)"} />);
    expect(container.querySelector("img")).toBeNull();
    const link = container.querySelector("a");
    expect(link?.getAttribute("href")).toBe("https://example.test/net.png");
    expect(link?.textContent).toBe("diagram");
  });

  it("keeps same-origin images", () => {
    const { container } = render(<MarkdownContent text={"![local](/static/images/net.png)"} />);
    expect(container.querySelector("img")?.getAttribute("src")).toBe("/static/images/net.png");
  });

  it("drops images when the caller disallows them (briefing policy)", () => {
    const { container } = render(
      <MarkdownContent text={"![diagram](https://example.test/net.png)"} disallowedElements={["img"]} />,
    );
    expect(container.querySelector("img")).toBeNull();
  });

  it("renders no image for an unsafe image source", () => {
    const { container } = render(<MarkdownContent text={"![x](javascript:alert(1))"} />);
    expect(container.querySelector("img")).toBeNull();
    expect(container.querySelector("a")).toBeNull();
  });

  it("neutralizes a scheme smuggled past a naive prefix test with an entity-encoded tab", () => {
    // `jav<TAB>ascript:` — a browser strips the tab and executes it; the URL
    // policy must reject it (empty href) before it reaches the DOM.
    const { container } = render(<MarkdownContent text={"[open](<jav&#x09;ascript:alert(1)>)"} />);
    expect(anchorHref(container)).toBe("");
  });

  it("neutralizes a scheme smuggled with an entity-encoded newline", () => {
    const { container } = render(<MarkdownContent text={"[open](<jav&#x0a;ascript:alert(1)>)"} />);
    expect(anchorHref(container)).toBe("");
  });
});

describe("communication Markdown profile", () => {
  it("removes images even when callers allow them", () => {
    const { container } = render(<MarkdownContent text="![x](https://docs.example.test/a.png)" profile="ctf-communication-markdown/v1" allowedLinkHosts={["docs.example.test"]} />);
    expect(container.querySelector("img")).toBeNull();
  });
  it.each(["https://untrusted.test/path", "//docs.example.test/path", "mailto:a@example.test", "/\\untrusted.test/path", "https://user:pass@docs.example.test/path"])("blocks %s", (url) => {
    const { container } = render(<MarkdownContent text={`[x](${url})`} profile="ctf-communication-markdown/v1" allowedLinkHosts={["docs.example.test"]} />);
    expect(anchorHref(container)).toBe("");
  });
  it.each(["/events/rules", "#rules", "https://docs.example.test/rules"])("permits %s", (url) => {
    const { container } = render(<MarkdownContent text={`[x](${url})`} profile="ctf-communication-markdown/v1" allowedLinkHosts={["docs.example.test"]} />);
    expect(anchorHref(container)).toBe(url);
  });
});
