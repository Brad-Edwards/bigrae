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
      // Keys are zod-validated enums (dev/prod) or internal iteration — not user input
      "security/detect-object-injection": "off",
    },
  },
  {
    files: ["audit.js", "policy-config.js"],
    rules: {
      // These modules deliberately operate on administrator-configured local
      // paths. audit.js opens through one descriptor and tightens permissions;
      // policy-config.js only reads the selected policy before strict parsing.
      "security/detect-non-literal-fs-filename": "off",
    },
  },
  {
    files: ["schemas.js"],
    rules: {
      // The reported request validators are anchored, linear character-class
      // patterns with closed maximum shapes; they contain no nested quantifier.
      "security/detect-unsafe-regex": "off",
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
