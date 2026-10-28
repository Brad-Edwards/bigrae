import { afterEach, describe, expect, it } from "vitest";

import { installStyleNonce, readCspNonce } from "./csp-nonce";

let uninstall: (() => void) | undefined;

function addNonceMeta(nonce: string): void {
  const meta = document.createElement("meta");
  meta.setAttribute("property", "csp-nonce");
  meta.setAttribute("nonce", nonce);
  document.head.append(meta);
}

afterEach(() => {
  uninstall?.();
  uninstall = undefined;
  document.head.innerHTML = "";
});

describe("readCspNonce", () => {
  it("returns the nonce the SPA host published", () => {
    addNonceMeta("abc123");
    expect(readCspNonce()).toBe("abc123");
  });

  it("returns undefined when the host published no nonce", () => {
    expect(readCspNonce()).toBeUndefined();
  });
});

describe("installStyleNonce", () => {
  it("stamps the nonce on script-created style elements only", () => {
    addNonceMeta("abc123");
    uninstall = installStyleNonce();

    expect(document.createElement("style").getAttribute("nonce")).toBe("abc123");
    expect(document.createElement("STYLE").getAttribute("nonce")).toBe("abc123");
    expect(document.createElement("script").hasAttribute("nonce")).toBe(false);
    expect(document.createElement("div").hasAttribute("nonce")).toBe(false);
  });

  it("does not stamp style markup that the parser creates", () => {
    addNonceMeta("abc123");
    uninstall = installStyleNonce();

    const host = document.createElement("div");
    host.innerHTML = "<style>body { color: red }</style>";
    expect(host.querySelector("style")?.hasAttribute("nonce")).toBe(false);
  });

  it("changes nothing when no nonce is published", () => {
    const original = document.createElement;
    uninstall = installStyleNonce();

    expect(document.createElement).toBe(original);
    expect(document.createElement("style").hasAttribute("nonce")).toBe(false);
  });

  it("restores the original factory on uninstall", () => {
    addNonceMeta("abc123");
    const original = document.createElement;
    installStyleNonce()();

    expect(document.createElement).toBe(original);
    expect(document.createElement("style").hasAttribute("nonce")).toBe(false);
  });
});
