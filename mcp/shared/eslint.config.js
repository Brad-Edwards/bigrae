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
];
