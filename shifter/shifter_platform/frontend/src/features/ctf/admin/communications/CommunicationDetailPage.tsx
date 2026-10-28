/** One campaign: delivery state per event and channel, recipients, timeline, and cancel (#2052). */
import { useState } from "react";
import { Link, useParams } from "react-router";

import {
  type DeliveryReport,
  useCancelCtfCommunication,
  useCommunication,
  useCommunicationActivity,
  useCommunicationDelivery,
  useCommunicationRecipients,
} from "@/api/ctfCommunications";
import { describeMutationError } from "@/api/errors";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
  AlertDialogTrigger,
} from "@/components/ui/alert-dialog";
import { PageHeader } from "@/components/page-header";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { ctfAdminCommunicationsPath } from "@/features/ctf/routes";

type Occurrence = DeliveryReport["results"][number];
type ChannelCounts = Occurrence["totals"]["channels"][number];

function channelSummary(channel: ChannelCounts): string {
  const failed = channel.permanent_failure + channel.expired;
  const pending = channel.queued + channel.claimed + channel.retry_due;
  const stopped = channel.cancelled + channel.suppressed;
  return `${channel.channel}: ${channel.accepted} accepted, ${pending} pending, ${failed} failed, ${stopped} stopped`;
}

function Retry({ label, onRetry }: Readonly<{ label: string; onRetry: () => void }>) {
  return (
    <Alert variant="destructive">
      <AlertDescription className="flex items-center gap-3">
        {label}
        <Button size="sm" variant="outline" onClick={onRetry}>
          Retry
        </Button>
      </AlertDescription>
    </Alert>
  );
}

function Recipients({ campaignId, intentId, eventId }: Readonly<{ campaignId: string; intentId: string; eventId: string }>) {
  const [offset, setOffset] = useState(0);
  const rows = useCommunicationRecipients(campaignId, intentId, eventId, offset);
  if (rows.isLoading) return <Skeleton className="h-20 w-full" />;
  if (rows.isError) return <Retry label="Could not load recipients." onRetry={() => void rows.refetch()} />;
  const page = rows.data?.results ?? [];
  return (
    <div className="space-y-2">
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>Participant</TableHead>
            <TableHead>Team</TableHead>
            <TableHead>Delivery</TableHead>
            <TableHead>Read</TableHead>
            <TableHead>Acknowledged</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {page.map((row) => (
            <TableRow key={row.participant_id}>
              <TableCell>{row.removed ? "Removed participant" : row.name}</TableCell>
              <TableCell>{row.team ?? "—"}</TableCell>
              <TableCell>{row.channels.map((channel) => `${channel.channel}: ${channel.status}`).join(", ")}</TableCell>
              <TableCell>{row.read_at ? new Date(row.read_at).toLocaleString() : "—"}</TableCell>
              <TableCell>{row.acknowledged_at ? new Date(row.acknowledged_at).toLocaleString() : "—"}</TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
      <div className="flex gap-2">
        <Button size="sm" variant="outline" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - 50))}>
          Previous recipients
        </Button>
        <Button size="sm" variant="outline" disabled={rows.data?.next_offset == null} onClick={() => setOffset(rows.data?.next_offset ?? offset)}>
          Next recipients
        </Button>
      </div>
    </div>
  );
}

function OccurrenceCard({ campaignId, occurrence }: Readonly<{ campaignId: string; occurrence: Occurrence }>) {
  const [openEvent, setOpenEvent] = useState("");
  const { totals } = occurrence;
  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex flex-wrap items-center gap-2 text-base">
          Revision {occurrence.revision_number}
          <Badge variant="secondary">{occurrence.status}</Badge>
          {occurrence.due_at ? <span className="text-sm font-normal">Due {new Date(occurrence.due_at).toISOString()} (UTC)</span> : null}
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-3">
        {occurrence.schedule_status === "failed" ? (
          <Alert variant="destructive">
            <AlertTitle>Release task failed</AlertTitle>
            <AlertDescription>The scheduler could not release this message. It has not been delivered.</AlertDescription>
          </Alert>
        ) : null}
        <p className="text-sm">
          {totals.recipients} recipients · {totals.inbox_available} in inbox · {totals.read} read · {totals.acknowledged} acknowledged
        </p>
        <ul className="text-sm text-muted-foreground">
          {totals.channels.map((channel) => (
            <li key={channel.channel}>{channelSummary(channel)}</li>
          ))}
        </ul>
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Event</TableHead>
              <TableHead className="text-right">Recipients</TableHead>
              <TableHead className="text-right">Read</TableHead>
              <TableHead className="text-right">Acknowledged</TableHead>
              <TableHead>Channels</TableHead>
              <TableHead>
                <span className="sr-only">Recipients</span>
              </TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {occurrence.events.map((event) => (
              <TableRow key={event.event_id}>
                <TableCell>{event.name}</TableCell>
                <TableCell className="text-right tabular-nums">{event.recipients}</TableCell>
                <TableCell className="text-right tabular-nums">{event.read}</TableCell>
                <TableCell className="text-right tabular-nums">{event.acknowledged}</TableCell>
                <TableCell>{event.channels.map(channelSummary).join("; ")}</TableCell>
                <TableCell>
                  <Button
                    size="sm"
                    variant="outline"
                    aria-expanded={openEvent === event.event_id}
                    disabled={event.recipients === 0}
                    onClick={() => setOpenEvent(openEvent === event.event_id ? "" : event.event_id)}
                  >
                    {openEvent === event.event_id ? `Hide recipients for ${event.name}` : `Show recipients for ${event.name}`}
                  </Button>
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
        {openEvent ? <Recipients campaignId={campaignId} intentId={occurrence.intent_id} eventId={openEvent} /> : null}
      </CardContent>
    </Card>
  );
}

function Activity({ campaignId }: Readonly<{ campaignId: string }>) {
  const activity = useCommunicationActivity(campaignId);
  if (activity.isLoading) return <Skeleton className="h-24 w-full" />;
  if (activity.isError) return <Retry label="Could not load the activity history." onRetry={() => void activity.refetch()} />;
  return (
    <ol className="space-y-1 text-sm">
      {activity.data?.entries.map((entry, index) => (
        <li key={`${entry.kind}-${entry.at}-${index}`}>
          <span className="font-medium">{entry.kind}</span>
          {entry.revision_number ? ` (revision ${entry.revision_number})` : ""}
          {entry.actor ? ` by ${entry.actor.label} via ${entry.actor.via}` : ""} at {new Date(entry.at).toLocaleString()}
        </li>
      ))}
      {activity.data?.truncated ? <li className="text-muted-foreground">Older entries are not shown.</li> : null}
    </ol>
  );
}

function CancelAction({ onConfirm }: Readonly<{ onConfirm: () => void }>) {
  return (
    <AlertDialog>
      <AlertDialogTrigger asChild>
        <Button variant="outline">Cancel unclaimed work</Button>
      </AlertDialogTrigger>
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle>Cancel this communication?</AlertDialogTitle>
          <AlertDialogDescription>
            Cancelling stops delivery work that no worker has claimed and stops a scheduled release. Messages already in
            the participant inbox, or accepted by a transport, stay delivered.
          </AlertDialogDescription>
        </AlertDialogHeader>
        <AlertDialogFooter>
          <AlertDialogCancel>Keep</AlertDialogCancel>
          <AlertDialogAction onClick={onConfirm}>Cancel unclaimed work</AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  );
}

export function CommunicationDetailPage() {
  const { campaignId = "" } = useParams();
  const campaign = useCommunication(campaignId);
  const delivery = useCommunicationDelivery(campaignId);
  const occurrences = delivery.data?.pages.flatMap((page) => page.results) ?? [];
  const cancel = useCancelCtfCommunication();
  const cancelError = describeMutationError(cancel.error, "Could not cancel the communication.");

  if (campaign.isLoading) return <Skeleton className="h-64 w-full" />;
  if (campaign.isError || !campaign.data) {
    return <Retry label="This communication is unavailable." onRetry={() => void campaign.refetch()} />;
  }
  const data = campaign.data;
  return (
    <div className="space-y-6">
      <Link className="text-sm underline-offset-4 hover:underline" to={ctfAdminCommunicationsPath()}>
        Back to communications
      </Link>
      <PageHeader
        title={data.title}
        description={
          <>
            <Badge variant="secondary">{data.status}</Badge> · {data.target_events.map((event) => event.name).join(", ")}
            {data.trigger.due_at ? ` · scheduled for ${new Date(data.trigger.due_at).toISOString()} (UTC)` : ""}
          </>
        }
        actions={
          data.status === "cancelled" ? null : (
            <CancelAction onConfirm={() => cancel.mutate(data.id, { onSuccess: () => void campaign.refetch() })} />
          )
        }
      />
      {cancelError ? (
        <Alert variant="destructive" role="alert">
          <AlertDescription>{cancelError}</AlertDescription>
        </Alert>
      ) : null}

      <section aria-labelledby="delivery-heading" className="space-y-3">
        <h2 id="delivery-heading" className="text-lg font-semibold">Delivery</h2>
        <p className="text-sm text-muted-foreground">
          Accepted means a transport took the message. Read and acknowledged come from participant actions.
        </p>
        {delivery.isLoading ? <Skeleton className="h-32 w-full" /> : null}
        {delivery.isError ? <Retry label="Could not load delivery state." onRetry={() => void delivery.refetch()} /> : null}
        {delivery.isSuccess && occurrences.length === 0 ? (
          <p className="text-sm text-muted-foreground">This communication has not been sent or scheduled.</p>
        ) : null}
        {occurrences.map((occurrence) => (
          <OccurrenceCard key={occurrence.intent_id} campaignId={data.id} occurrence={occurrence} />
        ))}
        {delivery.hasNextPage ? (
          <Button variant="outline" disabled={delivery.isFetchingNextPage} onClick={() => void delivery.fetchNextPage()}>
            Load older occurrences
          </Button>
        ) : null}
      </section>

      <section aria-labelledby="activity-heading" className="space-y-3">
        <h2 id="activity-heading" className="text-lg font-semibold">Activity</h2>
        <Activity campaignId={data.id} />
      </section>
    </div>
  );
}
