import security from "eslint-plugin-security";

export default [
  {
    files: ["**/*.js"],
    ignores: ["node_modules/**"],
    plugins: { security },
    rules: {
      ...Object.fromEntries(
        Object.keys(security.configs.recommended.rules).map((rule) => [rule, "error"]),
      ),
      // Plan/phase/step IDs are internally generated short UUIDs, not user input
      "security/detect-object-injection": "off",
    },
  },
  {
    files: ["lib.js"],
    rules: {
      // Plan IDs are validated as eight lowercase hex characters and resolved
      // beneath the configured plan directory before filesystem access. Other
      // paths come from readdir of that same directory.
      "security/detect-non-literal-fs-filename": "off",
    },
  },
  {
    files: ["**/*.test.js"],
    rules: {
      // Tests use node:test temporary directories and repository fixtures.
      "security/detect-non-literal-fs-filename": "off",
    },
  },
];
