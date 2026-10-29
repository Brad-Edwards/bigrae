/** Multi-event communications console: parent selection, compose, and campaign list (#2052). */
import { useState } from "react";
import { Link } from "react-router";

import { type CommunicationParent, useCommunicationTargets, useParentCommunications } from "@/api/ctfCommunications";
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

const WRAP = "whitespace-normal [overflow-wrap:anywhere]";

function CampaignList({ parent }: Readonly<{ parent: CommunicationParent }>) {
  const campaigns = useParentCommunications(parent);
  const rows = campaigns.data?.pages.flatMap((page) => page.results) ?? [];
  return (
    <Card>
      <CardHeader>
        <CardTitle>Communications for this selection</CardTitle>
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
          // Cells wrap so the table fits a narrow viewport; clipped columns in
          // the scroll container would hide text from sighted and axe readers.
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead className={WRAP}>Title</TableHead>
                <TableHead className={WRAP}>Status</TableHead>
                <TableHead className={WRAP}>Events</TableHead>
                <TableHead className={WRAP}>Created</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {rows.map((campaign) => (
                <TableRow key={campaign.id}>
                  <TableCell className={WRAP}>
                    <Link className="underline-offset-4 hover:underline" to={ctfAdminCommunicationPath(campaign.id)}>
                      {campaign.title}
                    </Link>
                  </TableCell>
                  <TableCell className={WRAP}>
                    <Badge variant="secondary">{campaign.status}</Badge>
                  </TableCell>
                  <TableCell className={WRAP}>{campaign.target_events.map((event) => event.name).join(", ")}</TableCell>
                  <TableCell className={WRAP}>{new Date(campaign.created_at).toLocaleString()}</TableCell>
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
  const [parentKey, setParentKey] = useState("");
  const all = useCommunicationTargets();
  const parents = all.data?.pages[0]?.parents ?? [];
  const active = selectedParent(parents, parentKey);
  const targets = useCommunicationTargets(active);
  const events = targets.data?.pages.flatMap((page) => page.events) ?? [];

  return (
    <div className="space-y-6">
      <PageHeader
        title="Communications"
        description="Send one message to participants in several events. Choose events in the same account, organization or workspace."
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
      {all.isSuccess && parents.length === 0 ? (
        <p className="text-sm text-muted-foreground">You cannot send communications to any event.</p>
      ) : null}
      {parents.length > 1 ? (
        <div className="max-w-sm space-y-2">
          <Label htmlFor="comm-parent">Account, organization or workspace</Label>
          <select
            id="comm-parent"
            className="h-9 w-full rounded-md border border-input bg-transparent px-3 text-sm"
            value={active ? keyFor(active) : ""}
            onChange={(change) => setParentKey(change.target.value)}
          >
            <option value="">Select an account, organization or workspace</option>
            {parents.map((parent) => (
              <option key={keyFor(parent)} value={keyFor(parent)}>
                {parent.name || parent.id} ({parent.kind})
              </option>
            ))}
          </select>
        </div>
      ) : null}
      {active && all.isSuccess && targets.isSuccess ? (
        <>
          <ComposeCommunication
            key={keyFor(active)}
            parent={active}
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
          <CampaignList parent={active} />
        </>
      ) : null}
      {active && targets.isError ? (
        <Alert variant="destructive"><AlertDescription>
          Could not load events for this selection.
          <Button variant="outline" size="sm" onClick={() => void targets.refetch()}>Retry events</Button>
        </AlertDescription></Alert>
      ) : null}
      {active && targets.isLoading ? <Skeleton className="h-64 w-full" /> : null}
    </div>
  );
}


function keyFor(parent: CommunicationParent): string {
  return `${parent.kind}:${parent.id}`;
}

function selectedParent(parents: CommunicationParent[], selectedKey: string): CommunicationParent | undefined {
  const selected = parents.find((parent) => keyFor(parent) === selectedKey);
  return selected ?? (selectedKey === "" && parents.length === 1 ? parents[0] : undefined);
}
