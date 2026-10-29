import { Outlet, useLocation, useMatches } from "react-router";

import { ShieldOff } from "lucide-react";

import { AppShell } from "@/components/app-shell";
import { NotFoundPage } from "@/components/not-found";
import { permissionAllows, type PermissionPolicy } from "@/app/nav";

// React Router decodes a percent-encoded separator back into a route param, and
// pages put those params into API paths. A link with an encoded "/", "\", "?",
// "#", or "%" in one segment could then redirect a state-changing request to
// another endpoint with the user's session and CSRF token. No SPA route carries
// these characters inside a segment, so such a URL is not a page.
const SMUGGLED_SEPARATOR = /%(?:2f|5c|3f|23|25)/i;

/** True when the raw (still-encoded) pathname could smuggle separators into route params. */
export function hasSmuggledSeparator(pathname: string): boolean {
  return SMUGGLED_SEPARATOR.test(pathname);
}

import { BootstrapProvider, useBootstrapContext } from "./bootstrap-context";
import { ModeProvider } from "./mode";

/** Route handle carrying the advisory permission policy for a SPA-owned route. */
export interface RouteHandle {
  readonly permissionPolicy?: PermissionPolicy;
}

function AccessDenied() {
  // Advisory only; does not reveal whether any specific resource exists. The API
  // remains the authoritative boundary and returns 403 regardless. This is the
  // canonical permission-denied workspace state reused across surfaces (#1368).
  return (
    <div className="grid place-items-center py-24 text-center">
      <div className="max-w-sm">
        <ShieldOff className="mx-auto mb-4 size-8 text-muted-foreground" />
        <h1 className="text-lg font-semibold tracking-tight">Access denied</h1>
        <p className="mt-1 text-sm text-muted-foreground">
          You do not have access to this area. Contact an administrator if you believe this is an error.
        </p>
      </div>
    </div>
  );
}

function RouteGate() {
  const bootstrap = useBootstrapContext();
  const matches = useMatches();
  const { pathname } = useLocation();
  // Most-specific matched route wins; its advisory policy gates the render.
  const policy = [...matches]
    .reverse()
    .map((match) => (match.handle as RouteHandle | undefined)?.permissionPolicy)
    .find((value): value is PermissionPolicy => Boolean(value));

  if (hasSmuggledSeparator(pathname)) {
    return <NotFoundPage />;
  }
  if (policy && !permissionAllows(policy, bootstrap)) {
    return <AccessDenied />;
  }
  return <Outlet />;
}

function WorkspaceFrame() {
  const bootstrap = useBootstrapContext();
  return (
    <ModeProvider bootstrap={bootstrap}>
      <AppShell>
        <RouteGate />
      </AppShell>
    </ModeProvider>
  );
}

export function RootLayout() {
  return (
    <BootstrapProvider>
      <WorkspaceFrame />
    </BootstrapProvider>
  );
}
