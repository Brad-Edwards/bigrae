// Behavioral guard for the AMI dispatcher's protected-ref boundary (#1656).
//
// resolveProtectedAmiRef is unit-tested directly in lib.test.js; this test
// pins the *wiring* in triggerAmiWorkflow, which regressed once already: PR1
// wired the gate in the pre-split mcp/ops/index.js, then the #690 module split
// re-created triggerAmiWorkflow in github-actions.js on the unprotected
// `ref ?? resolveGitRef(...)` path. A non-protected ref must be rejected before
// any workflow_dispatch is emitted, so a feature-branch copy cannot be used to
// weaken its own inline dev|main gate.

import { describe, it } from "node:test";
import assert from "node:assert/strict";

import { triggerAmiWorkflow, triggerGceImageWorkflow } from "./github-actions.js";

// Synthetic target repository (ADR-069: tests use synthetic identities).
const SYNTHETIC_REPO = "example-org/example-repo";

function makeRecordingRunner({ ghDefault = "" } = {}) {
  const calls = [];
  const runner = (argv, options) => {
    calls.push({ argv, options });
    const isDefaultView = argv.join(" ") === "repo set-default --view";
    return {
      status: 0,
      stdout: isDefaultView ? ghDefault : "",
      stderr: "",
      error: null,
    };
  };
  runner.calls = calls;
  return runner;
}

describe("triggerAmiWorkflow protected-ref dispatch (#1656)", () => {
  it("rejects non-protected / injected refs before dispatching", () => {
    for (const bad of [
      "feature-x",
      "1656-harden-packer-privilege",
      "refs/tags/dev",
      "dev; rm -rf /",
      "",
    ]) {
      assert.throws(
        () =>
          triggerAmiWorkflow({
            workflow: "packer.yml",
            ami_type: "kali",
            ref: bad,
            actionsPath: "packer.yml",
          }),
        /protected branch/,
        `expected ref '${bad}' to be rejected before dispatch`,
      );
    }
  });

  it("dispatches the expected workflow argv for a protected ref", () => {
    const runner = makeRecordingRunner();
    const message = triggerAmiWorkflow(
      {
        workflow: "packer.yml",
        ami_type: "kali",
        ref: "dev",
        actionsPath: "packer.yml",
      },
      { runner, token: "test-token", env: { GH_REPO: SYNTHETIC_REPO } },
    );

    assert.equal(runner.calls.length, 1);
    assert.deepEqual(runner.calls[0].argv, [
      "workflow",
      "run",
      "packer.yml",
      "--repo",
      SYNTHETIC_REPO,
      "--ref",
      "dev",
      "-f",
      "ami_type=kali",
    ]);
    assert.equal(
      message,
      `Triggered packer.yml for kali on ref dev. View at: https://github.com/${SYNTHETIC_REPO}/actions/workflows/packer.yml`,
    );
  });
});

describe("workflow dispatch target repository (ADR-069)", () => {
  const amiRequest = {
    workflow: "packer.yml",
    ami_type: "kali",
    ref: "dev",
    actionsPath: "packer.yml",
  };

  it("dispatches to the gh default repository when GH_REPO is unset", () => {
    const runner = makeRecordingRunner({ ghDefault: `${SYNTHETIC_REPO}\n` });
    triggerAmiWorkflow(amiRequest, { runner, token: "test-token", env: {} });

    assert.deepEqual(
      runner.calls.map((call) => call.argv.slice(0, 3)),
      [
        ["repo", "set-default", "--view"],
        ["workflow", "run", "packer.yml"],
      ],
    );
    const dispatch = runner.calls[1].argv;
    assert.equal(dispatch[dispatch.indexOf("--repo") + 1], SYNTHETIC_REPO);
  });

  it("fails closed without GH_REPO or a gh default, before any dispatch", () => {
    const gceRequest = {
      workflow: "packer-gcp.yml",
      inputs: { image_type: "ubuntu" },
      ref: "dev",
      actionsPath: "packer-gcp.yml",
    };
    for (const trigger of [
      (ghOptions) => triggerAmiWorkflow(amiRequest, ghOptions),
      (ghOptions) => triggerGceImageWorkflow(gceRequest, ghOptions),
    ]) {
      const runner = makeRecordingRunner();
      assert.throws(
        () => trigger({ runner, token: "test-token", env: {} }),
        /GitHub repository not configured/,
      );
      assert.deepEqual(
        runner.calls.map((call) => call.argv),
        [["repo", "set-default", "--view"]],
      );
    }
  });
});

describe("triggerGceImageWorkflow protected-ref dispatch", () => {
  it("rejects non-protected refs before credentialed workflow dispatch", () => {
    assert.throws(
      () =>
        triggerGceImageWorkflow({
          workflow: "packer-gcp.yml",
          inputs: { image_type: "ubuntu" },
          ref: "feature-image-build",
          actionsPath: "packer-gcp.yml",
        }),
      /protected branch/,
    );
  });

  it("dispatches every supplied input for a protected ref", () => {
    const runner = makeRecordingRunner();
    const message = triggerGceImageWorkflow(
      {
        workflow: "packer-gcp.yml",
        inputs: {
          image_type: "dc-prebaked",
          dc_profile: "example",
        },
        ref: "main",
        actionsPath: "packer-gcp.yml",
      },
      { runner, token: "test-token", env: { GH_REPO: SYNTHETIC_REPO } },
    );

    assert.equal(runner.calls.length, 1);
    assert.deepEqual(runner.calls[0].argv, [
      "workflow",
      "run",
      "packer-gcp.yml",
      "--repo",
      SYNTHETIC_REPO,
      "--ref",
      "main",
      "-f",
      "image_type=dc-prebaked",
      "-f",
      "dc_profile=example",
    ]);
    assert.equal(
      message,
      `Triggered packer-gcp.yml for dc-prebaked on ref main. View at: https://github.com/${SYNTHETIC_REPO}/actions/workflows/packer-gcp.yml`,
    );
  });
});
