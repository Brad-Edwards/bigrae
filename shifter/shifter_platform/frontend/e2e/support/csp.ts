// Content Security Policy guard for the hermetic browser lanes (ADR-036-R2).
//
// The portal enforces one deny-by-default CSP. A blocked script, style, or
// connection is a product defect in enforce mode: the feature silently stops
// working. Every journey and accessibility scan therefore records the browser's
// `securitypolicyviolation` events and fails on any of them.
//
// Diagnostics are bounded (ADR-055-R7): the effective directive, the blocked
// resource as `inline`/`eval` or origin+path, and the source file origin+path
// with its line. Query strings, fragments, samples, and DOM content are never
// recorded.

import { test as base, expect, type Browser, type BrowserContext } from "@playwright/test";

export interface CspViolation {
  readonly directive: string;
  readonly blocked: string;
  readonly source: string;
  readonly line: number;
}

const BINDING = "__recordCspViolation";

/** Record CSP violations for every page in `context` into `sink`. */
export async function guardCsp(context: BrowserContext, sink: CspViolation[] = []): Promise<CspViolation[]> {
  await context.exposeBinding(BINDING, (_source, violation: CspViolation) => {
    sink.push(violation);
  });
  await context.addInitScript((binding: string) => {
    const strip = (raw: string): string => {
      if (!raw?.includes(":")) {
        return raw;
      }
      try {
        const url = new URL(raw);
        return `${url.origin}${url.pathname}`;
      } catch {
        return "unparseable";
      }
    };
    document.addEventListener("securitypolicyviolation", (event) => {
      const record = (window as unknown as Record<string, (v: unknown) => void>)[binding];
      record({
        directive: event.effectiveDirective,
        blocked: strip(event.blockedURI),
        source: strip(event.sourceFile),
        line: event.lineNumber,
      });
    });
  }, BINDING);
  return sink;
}

interface CspFixtures {
  /** Violations from the test's default context and every guarded context. */
  cspViolations: CspViolation[];
  /** Create a browser context that records violations into `cspViolations`. */
  newGuardedContext: (options?: Parameters<Browser["newContext"]>[0]) => Promise<BrowserContext>;
  /** Auto fixture: fails the test when any guarded context saw a violation. */
  cspGuard: void;
}

export const test = base.extend<CspFixtures>({
  cspViolations: async ({ context }, use) => {
    await use(await guardCsp(context));
  },
  newGuardedContext: async ({ browser, cspViolations }, use) => {
    const created: BrowserContext[] = [];
    await use(async (options) => {
      const context = await browser.newContext(options);
      created.push(context);
      await guardCsp(context, cspViolations);
      return context;
    });
    await Promise.all(created.map((context) => context.close()));
  },
  cspGuard: [
    async ({ cspViolations }, use) => {
      await use();
      expect(cspViolations, "Content Security Policy violations").toEqual([]);
    },
    { auto: true },
  ],
});

export { expect };
