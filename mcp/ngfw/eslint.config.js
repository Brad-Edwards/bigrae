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
    files: ["tool-surface.test.js"],
    rules: {
      // The test assembles a regular expression from its closed, static tool-name
      // inventory to verify registration syntax; no runtime input reaches it.
      "security/detect-non-literal-regexp": "off",
    },
  },
];
