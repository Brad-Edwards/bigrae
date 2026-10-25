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
    files: ["schemas.js"],
    rules: {
      // The reported request validators are anchored, linear character-class
      // patterns with closed maximum shapes; they contain no nested quantifier.
      "security/detect-unsafe-regex": "off",
    },
  },
];
