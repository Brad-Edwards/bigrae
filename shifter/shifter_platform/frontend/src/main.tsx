import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import { QueryClientProvider } from "@tanstack/react-query";
import { RouterProvider } from "react-router";

import "./index.css";

import { createQueryClient } from "@/api/queryClient";
import { installStyleNonce } from "@/lib/csp-nonce";
import { applyInitialTheme } from "@/lib/theme";
import { router } from "@/router";

// Before any component mounts: libraries create <style> elements at run time
// and the enforced CSP admits them only with the response nonce (ADR-036).
installStyleNonce();
applyInitialTheme();

const queryClient = createQueryClient();
const container = document.getElementById("root");

if (container) {
  createRoot(container).render(
    <StrictMode>
      <QueryClientProvider client={queryClient}>
        <RouterProvider router={router} />
      </QueryClientProvider>
    </StrictMode>,
  );
}
