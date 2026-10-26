/* eslint-disable security/detect-non-literal-fs-filename -- repository fixture and temporary files created by this test */
import { describe, it } from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, writeFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { loadPolicy, profileFromEnv, PolicyError } from "./policy-config.js";

describe("public product profile aliases", () => {
  it("reads the canonical profile and retains the legacy profile", () => {
    assert.equal(profileFromEnv({ BIGRAE_OPS_PROFILE: "operator" }), "operator");
    assert.equal(profileFromEnv({ SHIFTER_OPS_PROFILE: "operator" }), "operator");
  });
  it("rejects conflicting profiles before enabling any capabilities", () => {
    assert.throws(
      () => profileFromEnv({ BIGRAE_OPS_PROFILE: "reader", SHIFTER_OPS_PROFILE: "operator" }),
      PolicyError,
    );
  });
});

describe("policy file compatibility", () => {
  it("loads the legacy file when the canonical path is absent, and rejects differing policies", () => {
    const root = mkdtempSync(join(tmpdir(), "bigrae-policy-test-"));
    try {
      const canonical = join(root, ".bigrae.yaml");
      const legacy = join(root, ".shifter.yaml");
      const contents = readFileSync(new URL("../../.shifter.yaml", import.meta.url), "utf8");
      writeFileSync(legacy, contents);
      const input = { path: canonical, compatibilityPath: legacy };
      assert.ok(loadPolicy(input));
      writeFileSync(canonical, contents);
      assert.ok(loadPolicy(input));
      writeFileSync(canonical, contents.replace("version: 1", "version: 999"));
      assert.throws(() => loadPolicy(input), PolicyError);
    } finally {
      rmSync(root, { recursive: true, force: true });
    }
  });
});
