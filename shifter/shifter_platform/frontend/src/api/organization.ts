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

export function useAdministrableOrganizations(page = 1, integration: "adapters" | "model_sources" = "adapters") {
  const pageSize = 25;
  return useQuery({
    queryKey: [...organizationKeys.administrable, integration, page],
    queryFn: async ({ signal }): Promise<OrganizationPage> => {
      const result = await apiFetch<Pick<OrganizationPage, "count" | "results">>("/administer/organizations/", {
        signal,
        query: { integration, offset: (page - 1) * pageSize, limit: pageSize },
      });
      return {
        ...result,
        next: page * pageSize < result.count ? String(page + 1) : null,
        previous: page > 1 ? String(page - 1) : null,
      };
    },
  });
}
