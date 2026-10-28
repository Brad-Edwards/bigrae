/** Multi-event communications console: workspace selection, compose, and campaign list (#2052). */
import { useState } from "react";
import { Link } from "react-router";

import { useCommunicationTargets, useWorkspaceCommunications } from "@/api/ctfCommunications";
import { PageHeader } from "@/components/page-header";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Label } from "@/components/ui/label";
import { Skeleton } from "@/components/ui/skeleton";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { ctfAdminCommunicationPath } from "@/features/ctf/routes";

import { ComposeCommunication } from "./ComposeCommunication";

function CampaignList({ workspaceId }: Readonly<{ workspaceId: string }>) {
  const campaigns = useWorkspaceCommunications(workspaceId);
  const rows = campaigns.data?.pages.flatMap((page) => page.results) ?? [];
  return (
    <Card>
      <CardHeader>
        <CardTitle>Communications in this workspace</CardTitle>
      </CardHeader>
      <CardContent className="space-y-3">
        {campaigns.isLoading ? <Skeleton className="h-24 w-full" /> : null}
        {campaigns.isError ? (
          <Alert variant="destructive">
            <AlertDescription className="flex items-center gap-3">
              Could not load communications.
              <Button size="sm" variant="outline" onClick={() => void campaigns.refetch()}>
                Retry
              </Button>
            </AlertDescription>
          </Alert>
        ) : null}
        {campaigns.isSuccess && rows.length === 0 ? (
          <p className="text-sm text-muted-foreground">No communications yet.</p>
        ) : null}
        {rows.length > 0 ? (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Title</TableHead>
                <TableHead>Status</TableHead>
                <TableHead>Events</TableHead>
                <TableHead>Created</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {rows.map((campaign) => (
                <TableRow key={campaign.id}>
                  <TableCell>
                    <Link className="underline-offset-4 hover:underline" to={ctfAdminCommunicationPath(campaign.id)}>
                      {campaign.title}
                    </Link>
                  </TableCell>
                  <TableCell>
                    <Badge variant="secondary">{campaign.status}</Badge>
                  </TableCell>
                  <TableCell>{campaign.target_events.map((event) => event.name).join(", ")}</TableCell>
                  <TableCell>{new Date(campaign.created_at).toLocaleString()}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        ) : null}
        {campaigns.hasNextPage ? (
          <Button variant="outline" disabled={campaigns.isFetchingNextPage} onClick={() => void campaigns.fetchNextPage()}>
            Load more communications
          </Button>
        ) : null}
      </CardContent>
    </Card>
  );
}

export function CommunicationsPage() {
  const [workspaceId, setWorkspaceId] = useState("");
  const all = useCommunicationTargets();
  const workspaces = all.data?.pages[0]?.workspaces ?? [];
  const active = workspaceId || (workspaces.length === 1 ? workspaces[0].id : "");
  const targets = useCommunicationTargets(active);
  const events = targets.data?.pages.flatMap((page) => page.events) ?? [];

  return (
    <div className="space-y-6">
      <PageHeader
        title="Communications"
        description="Send one message to participants in several events. A message reaches only events in one workspace."
      />
      {all.isLoading ? <Skeleton className="h-10 w-64" /> : null}
      {all.isError ? (
        <Alert variant="destructive">
          <AlertTitle>Could not load your events</AlertTitle>
          <AlertDescription className="flex items-center gap-3">
            Please retry.
            <Button size="sm" variant="outline" onClick={() => void all.refetch()}>
              Retry
            </Button>
          </AlertDescription>
        </Alert>
      ) : null}
      {all.isSuccess && workspaces.length === 0 ? (
        <p className="text-sm text-muted-foreground">You cannot send communications in any workspace.</p>
      ) : null}
      {workspaces.length > 1 ? (
        <div className="max-w-sm space-y-2">
          <Label htmlFor="comm-workspace">Workspace</Label>
          <select
            id="comm-workspace"
            className="h-9 w-full rounded-md border border-input bg-transparent px-3 text-sm"
            value={active}
            onChange={(change) => setWorkspaceId(change.target.value)}
          >
            <option value="">Select a workspace</option>
            {workspaces.map((workspace) => (
              <option key={workspace.id} value={workspace.id}>
                {workspace.name || workspace.id}
              </option>
            ))}
          </select>
        </div>
      ) : null}
      {active && targets.data ? (
        <>
          <ComposeCommunication
            key={active}
            workspaceId={active}
            events={events}
            moreEvents={
              targets.hasNextPage ? (
                <Button
                  size="sm"
                  variant="outline"
                  type="button"
                  disabled={targets.isFetchingNextPage}
                  onClick={() => void targets.fetchNextPage()}
                >
                  Load more events
                </Button>
              ) : null
            }
          />
          <CampaignList workspaceId={active} />
        </>
      ) : null}
      {active && targets.isLoading ? <Skeleton className="h-64 w-full" /> : null}
    </div>
  );
}
