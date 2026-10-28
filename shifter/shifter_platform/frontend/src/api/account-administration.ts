/** Dormant S4 clients; DRF's prepared contract remains the type source. */
import { useState } from "react";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { apiFetch, type QueryValue } from "./client";
import type { components } from "./account-admin-schema";

export type AdministrationSchemas = components["schemas"];
export type Account = AdministrationSchemas["AccountView"];
export type Organization = AdministrationSchemas["OrganizationView"];
export type Workspace = AdministrationSchemas["WorkspaceView"];
export type MembershipPage = AdministrationSchemas["AccountMemberPage"];
export type Scope = Readonly<{ kind: "account" | "organization" | "workspace"; uuid: string }>;

const scopeCollections = { account: "accounts", organization: "organizations", workspace: "workspaces" } as const;
export const scopePath = (scope: Scope) => `/${scopeCollections[scope.kind]}/${encodeURIComponent(scope.uuid)}`;

export function useAdministrationQuery<T>(path: string, query?: Record<string, QueryValue>, enabled = true) {
  return useQuery({
    queryKey: ["account-administration", path, query],
    enabled: enabled && Boolean(path),
    queryFn: ({ signal }) => apiFetch<T>(path, { query, signal }),
  });
}

export function useAdministrationMutation<T = unknown, B = unknown>(path: string, method = "POST") {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (body: B) => apiFetch<T>(path, { method, body }),
    retry: false,
    onSuccess: () => client.invalidateQueries({ queryKey: ["account-administration"] }),
  });
}

/** Resume an accepted creation using its server-issued receipt and original body. */
export function useHierarchyCreation<T, B extends object>(path: string) {
  type Pending = { uuid: string; publication_receipt: string; publication_state: "pending" };
  const [pendingPublication, setPendingPublication] = useState<Pending | null>(null);
  const [declaration, setDeclaration] = useState<B | null>(null);
  const create = useAdministrationMutation<T | Pending, B & { publication_receipt?: string }>(path);
  const accept = (result: T | Pending) => {
    if (result && typeof result === "object" && "publication_state" in result && result.publication_state === "pending") {
      setPendingPublication(result as Pending);
    } else {
      setPendingPublication(null);
      setDeclaration(null);
    }
  };
  return {
    ...create,
    pendingPublication,
    mutate: (body: B) => {
      if (pendingPublication) return;
      setDeclaration(body);
      create.mutate(body, { onSuccess: accept });
    },
    retryPublication: () => {
      if (pendingPublication && declaration) {
        create.mutate({ ...declaration, publication_receipt: pendingPublication.publication_receipt }, { onSuccess: accept });
      }
    },
  };
}
