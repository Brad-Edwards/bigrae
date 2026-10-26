/** Policy-filtered organization chooser for organization-owned admin surfaces. */
import { useQuery } from "@tanstack/react-query";

import { apiFetch } from "./client";
import type { Organization } from "./account-administration";

export const organizationKeys = {
  administrable: ["accounts", "organizations", "policy-visible"] as const,
};

interface OrganizationPage {
  count: number;
  next: string | null;
  previous: string | null;
  results: Organization[];
}

export function useAdministrableOrganizations(page = 1) {
  const pageSize = 25;
  return useQuery({
    queryKey: [...organizationKeys.administrable, page],
    queryFn: ({ signal }) => apiFetch<OrganizationPage>("/workspaces/organizations/", {
      signal,
      query: { offset: (page - 1) * pageSize, limit: pageSize },
    }),
  });
}
