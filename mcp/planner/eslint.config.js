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
];
