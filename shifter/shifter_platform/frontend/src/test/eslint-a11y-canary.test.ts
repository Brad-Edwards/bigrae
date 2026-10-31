import path from "node:path";

import { ESLint } from "eslint";
import { describe, expect, it } from "vitest";

/**
 * Accessibility-lint canary (#1880).
 *
 * The accessibility plugin calls `minimatch` from its label rules. The
 * original `eslint-plugin-jsx-a11y@6.10.2` called a default export that
 * `minimatch@10` and later do not have, so a `minimatch` upgrade stopped
 * accessibility linting with `TypeError: (0 , _minimatch.default) is not a
 * function`. The SPA now uses the `eslint-plugin-jsx-a11y-x` fork, which uses
 * the named export (#2313).
 *
 * A `minimatch` upgrade must not stop accessibility linting silently, so this
 * canary runs the SPA's real `eslint.config.js` and asserts both halves of it:
 *
 *   - the `<img>` with no `alt` still produces `jsx-a11y-x/alt-text`, proving
 *     the accessibility rules are loaded and firing rather than quietly inert;
 *   - the `<label>` wrapping an `<input>` drives `label-has-associated-control`
 *     through `mayContainChildComponent` -> `minimatch`, which is the exact
 *     call path that breaks. A fixture containing only the `<img>` would still
 *     pass against a broken tree, so the label element is load-bearing — do not
 *     remove it.
 */
const CANARY_SOURCE = `export function LintCanary() {
  return (
    <form>
      <label>
        Name
        <input type="text" />
      </label>
      <img src="/canary.png" />
    </form>
  );
}
`;

// Vitest runs with the SPA package root as its working directory, which is
// also where `eslint.config.js` lives, so ESLint resolves the real config.
const FRONTEND_ROOT = process.cwd();

// Never written to disk. The path only has to match the `src/**/*.{ts,tsx}`
// block in `eslint.config.js` so the fixture picks up the accessibility rules.
const FIXTURE_PATH = path.join(FRONTEND_ROOT, "src", "lint-canary-fixture.tsx");

describe("jsx-a11y lint canary", () => {
  it("keeps accessibility rules firing under the SPA's real ESLint config", async () => {
    const eslint = new ESLint({ cwd: FRONTEND_ROOT });

    let results: ESLint.LintResult[];
    try {
      results = await eslint.lintText(CANARY_SOURCE, { filePath: FIXTURE_PATH });
    } catch (error) {
      // A rule crash propagates out of `lintText`, so accessibility linting is
      // broken outright rather than merely silent.
      throw new Error(
        "ESLint crashed while linting the accessibility canary, so accessibility " +
          "linting is broken repository-wide. This is the failure mode described in " +
          `issue #1880 — check whether minimatch was upgraded under ` +
          `eslint-plugin-jsx-a11y-x. Original error: ${(error as Error).message}`,
      );
    }

    expect(results).toHaveLength(1);
    const [result] = results;

    // A parse or config failure would otherwise masquerade as "no violations".
    expect(result.messages.filter((message) => message.fatal)).toEqual([]);

    expect(result.messages.map((message) => message.ruleId)).toEqual(["jsx-a11y-x/alt-text"]);
  }, 30_000);
});
