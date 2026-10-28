/**
 * Compose, preview, and send one communication to events in one workspace (#2052).
 *
 * The draft locks its targets, channel, acknowledgement, and timing when it is
 * saved, because the server pins them on the campaign. Content edits append an
 * immutable revision. Send releases exactly the revision the organizer reviewed.
 */
import { type ReactNode, useState } from "react";
import { useNavigate } from "react-router";

import {
  type AcknowledgementPolicy,
  type Campaign,
  type CommunicationPreview,
  type CommunicationTargets,
  useCancelCtfCommunication,
  useCommunicationPreview,
  useCreateCommunicationDraft,
  useReleaseCommunication,
  useReviseCommunication,
} from "@/api/ctfCommunications";
import { describeMutationError } from "@/api/errors";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Skeleton } from "@/components/ui/skeleton";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Textarea } from "@/components/ui/textarea";
import { MarkdownContent } from "@/features/ctf/MarkdownContent";
import { ctfAdminCommunicationPath } from "@/features/ctf/routes";

import { browserTimeZone, formatInZone, zonedWallTimeToUtc } from "./time";

type TargetEvent = CommunicationTargets["events"][number];

const NATIVE_SELECT =
  "h-9 w-full rounded-md border border-input bg-transparent px-3 text-sm shadow-xs focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-50";

const TIME_ERRORS = {
  invalid: "Enter a valid date, time, and time zone.",
  nonexistent: "This time does not exist in the selected time zone (clock change). Choose another time.",
  ambiguous: "This time occurs twice in the selected time zone (clock change). Choose another time.",
} as const;

function useSchedule(events: TargetEvent[]) {
  const [mode, setMode] = useState<"now" | "schedule">("now");
  const [date, setDate] = useState("");
  const [time, setTime] = useState("");
  const [zone, setZone] = useState(browserTimeZone());
  const zones = [...new Set(["UTC", browserTimeZone(), ...events.map((event) => event.event_timezone)])];
  const conversion = mode === "schedule" ? zonedWallTimeToUtc(date, time, zone) : null;
  let error: string | null = null;
  if (conversion && !conversion.ok) error = date && time ? TIME_ERRORS[conversion.reason] : "Enter a date and time.";
  else if (conversion?.ok && conversion.utc.getTime() <= Date.now()) error = "Choose a time in the future.";
  const dueAt = conversion?.ok && !error ? conversion.utc : null;
  return { mode, setMode, date, setDate, time, setTime, zone, setZone, zones, dueAt, error };
}

function PreviewPanel({ preview }: Readonly<{ preview: ReturnType<typeof useCommunicationPreview> }>) {
  if (preview.isLoading) return <Skeleton className="h-40 w-full" />;
  if (preview.isError || !preview.data) {
    return (
      <Alert variant="destructive">
        <AlertTitle>Could not load the preview</AlertTitle>
        <AlertDescription className="flex items-center gap-3">
          {describeMutationError(preview.error, "Please retry.")}
          <Button size="sm" variant="outline" onClick={() => void preview.refetch()}>
            Retry preview
          </Button>
        </AlertDescription>
      </Alert>
    );
  }
  const data = preview.data;
  return (
    <section aria-label="Preview" className="space-y-4">
      <div className="rounded-md border p-4">
        <p className="text-sm text-muted-foreground">Revision {data.revision.revision_number}</p>
        <h3 className="text-base font-semibold">{data.revision.subject}</h3>
        <MarkdownContent text={data.revision.body} profile="ctf-communication-markdown/v1" />
      </div>
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>Event</TableHead>
            <TableHead className="text-right">Current recipients</TableHead>
            {data.trigger.due_at ? <TableHead>Local send time</TableHead> : null}
          </TableRow>
        </TableHeader>
        <TableBody>
          {data.events.map((event) => (
            <TableRow key={event.event_id}>
              <TableCell>{event.name}</TableCell>
              <TableCell className="text-right tabular-nums">{event.recipient_count}</TableCell>
              {data.trigger.due_at ? (
                <TableCell>{formatInZone(new Date(data.trigger.due_at), event.event_timezone)}</TableCell>
              ) : null}
            </TableRow>
          ))}
        </TableBody>
      </Table>
      <p className="text-sm">
        Total: <span className="font-medium tabular-nums">{data.recipient_total}</span> recipients.
        {data.trigger.due_at ? ` Sends at ${new Date(data.trigger.due_at).toISOString()} (UTC).` : " Sends when you select Send now."}
      </p>
      <p className="text-sm text-muted-foreground">
        Counts are estimates at {formatInZone(new Date(data.observed_at), browserTimeZone())}. The platform resolves
        recipients when it releases the message. Preview sends nothing.
      </p>
      {data.channels.some((channel) => !channel.available) ? (
        <Alert variant="destructive">
          <AlertDescription>A selected channel is unavailable. You cannot send this message.</AlertDescription>
        </Alert>
      ) : null}
      {data.exceeds_audience_cap ? (
        <Alert variant="destructive">
          <AlertDescription>The audience exceeds the limit of {data.audience_cap} recipients. You cannot send this message.</AlertDescription>
        </Alert>
      ) : null}
    </section>
  );
}

/** Release only the revision whose preview loaded successfully and is within limits. */
function isReleasable(preview: CommunicationPreview | undefined, campaignId: string | undefined, revisionId: string) {
  return Boolean(
    preview &&
      preview.campaign_id === campaignId &&
      preview.revision.id === revisionId &&
      !preview.exceeds_audience_cap &&
      preview.channels.every((channel) => channel.available),
  );
}

function firstWriteError(errors: [unknown, string][]): string | null {
  for (const [error, fallback] of errors) {
    const message = describeMutationError(error, fallback);
    if (message) return message;
  }
  return null;
}

function EventPicker({
  events,
  selected,
  onToggle,
  disabled,
  more,
}: Readonly<{
  events: TargetEvent[];
  selected: string[];
  onToggle: (eventId: string, checked: boolean) => void;
  disabled: boolean;
  more?: ReactNode;
}>) {
  return (
    <fieldset disabled={disabled} className="space-y-2">
      <legend className="text-sm font-medium">Events</legend>
      {events.length === 0 ? (
        <p className="text-sm text-muted-foreground">You cannot send communications to any event in this workspace.</p>
      ) : null}
      {events.map((event) => (
        <div key={event.id} className="flex items-center gap-2">
          <input
            id={`target-${event.id}`}
            type="checkbox"
            checked={selected.includes(event.id)}
            onChange={(change) => onToggle(event.id, change.target.checked)}
          />
          <Label htmlFor={`target-${event.id}`} className="font-normal">
            {event.name} <span className="text-muted-foreground">({event.status}, {event.event_timezone})</span>
          </Label>
        </div>
      ))}
      {disabled ? null : more}
    </fieldset>
  );
}

function DeliverySettings({
  disabled,
  acknowledgement,
  onAcknowledgement,
}: Readonly<{
  disabled: boolean;
  acknowledgement: AcknowledgementPolicy;
  onAcknowledgement: (policy: AcknowledgementPolicy) => void;
}>) {
  return (
    <fieldset disabled={disabled} className="grid gap-4 sm:grid-cols-2">
      <legend className="sr-only">Delivery settings</legend>
      <div className="space-y-2">
        <Label htmlFor="comm-ack">Acknowledgement</Label>
        <select
          id="comm-ack"
          className={NATIVE_SELECT}
          value={acknowledgement}
          onChange={(change) => onAcknowledgement(change.target.value as AcknowledgementPolicy)}
        >
          <option value="none">None</option>
          <option value="read">Read</option>
          <option value="explicit">Explicit acknowledgement</option>
        </select>
      </div>
      <div className="space-y-2">
        <span className="text-sm font-medium">Channels</span>
        <div className="flex items-center gap-2">
          <input id="comm-in-app" type="checkbox" checked readOnly aria-describedby="comm-in-app-note" />
          <Label htmlFor="comm-in-app" className="font-normal">In-app inbox</Label>
        </div>
        <div className="flex items-center gap-2">
          <input id="comm-email" type="checkbox" disabled aria-describedby="comm-email-note" />
          <Label htmlFor="comm-email" className="font-normal">Email</Label>
        </div>
        <p id="comm-email-note" className="text-xs text-muted-foreground">Email is unavailable until the email transport is installed.</p>
        <p id="comm-in-app-note" className="sr-only">The in-app inbox is always selected.</p>
      </div>
    </fieldset>
  );
}

function ScheduleFields({
  disabled,
  schedule,
  selectedEvents,
}: Readonly<{ disabled: boolean; schedule: ReturnType<typeof useSchedule>; selectedEvents: TargetEvent[] }>) {
  const { dueAt } = schedule;
  return (
    <fieldset disabled={disabled} className="space-y-3">
      <legend className="text-sm font-medium">When to send</legend>
      <div className="flex gap-6">
        <label className="flex items-center gap-2 text-sm">
          <input type="radio" name="comm-timing" checked={schedule.mode === "now"} onChange={() => schedule.setMode("now")} />
          <span>Send now</span>
        </label>
        <label className="flex items-center gap-2 text-sm">
          <input
            type="radio"
            name="comm-timing"
            checked={schedule.mode === "schedule"}
            onChange={() => schedule.setMode("schedule")}
          />
          <span>Schedule</span>
        </label>
      </div>
      {schedule.mode === "schedule" ? (
        <div className="grid gap-3 sm:grid-cols-3">
          <div className="space-y-2">
            <Label htmlFor="comm-date">Date</Label>
            <Input id="comm-date" type="date" value={schedule.date} onChange={(e) => schedule.setDate(e.target.value)} />
          </div>
          <div className="space-y-2">
            <Label htmlFor="comm-time">Time</Label>
            <Input id="comm-time" type="time" value={schedule.time} onChange={(e) => schedule.setTime(e.target.value)} />
          </div>
          <div className="space-y-2">
            <Label htmlFor="comm-zone">Time zone</Label>
            <select id="comm-zone" className={NATIVE_SELECT} value={schedule.zone} onChange={(e) => schedule.setZone(e.target.value)}>
              {schedule.zones.map((zone) => (
                <option key={zone} value={zone}>
                  {zone}
                </option>
              ))}
            </select>
          </div>
          <div className="sm:col-span-3" aria-live="polite">
            {schedule.error ? <p className="text-sm text-destructive">{schedule.error}</p> : null}
            {dueAt ? (
              <ul className="text-sm">
                <li>UTC: {dueAt.toISOString()}</li>
                {selectedEvents.map((event) => (
                  <li key={event.id}>
                    {event.name}: {formatInZone(dueAt, event.event_timezone)}
                  </li>
                ))}
              </ul>
            ) : null}
          </div>
        </div>
      ) : null}
    </fieldset>
  );
}

export function ComposeCommunication({
  workspaceId,
  events,
  moreEvents,
}: Readonly<{ workspaceId: string; events: TargetEvent[]; moreEvents?: ReactNode }>) {
  const navigate = useNavigate();
  const [selected, setSelected] = useState<string[]>([]);
  const [title, setTitle] = useState("");
  const [subject, setSubject] = useState("");
  const [body, setBody] = useState("");
  const [acknowledgement, setAcknowledgement] = useState<AcknowledgementPolicy>("none");
  const [campaign, setCampaign] = useState<Campaign | null>(null);
  const [reviewed, setReviewed] = useState({ subject: "", body: "" });
  const selectedEvents = events.filter((event) => selected.includes(event.id));
  const schedule = useSchedule(selectedEvents);
  const create = useCreateCommunicationDraft();
  const revise = useReviseCommunication(campaign?.id ?? "");
  const release = useReleaseCommunication();
  const discard = useCancelCtfCommunication();
  const locked = campaign !== null;
  const revisionId = campaign?.latest_revision?.id ?? "";
  const contentChanged = locked && (subject.trim() !== reviewed.subject || body.trim() !== reviewed.body);
  const preview = useCommunicationPreview(campaign?.id ?? "", revisionId);
  const previewed = isReleasable(preview.data, campaign?.id, revisionId);
  const canSave = Boolean(selected.length > 0 && title.trim() && subject.trim() && body.trim() && !schedule.error);
  const writeError = firstWriteError([
    [create.error, "Could not save the draft."],
    [revise.error, "Could not update the draft."],
    [release.error, "Could not send the communication. Retry sends the same message."],
    [discard.error, "Could not discard the draft."],
  ]);

  function toggleEvent(eventId: string, checked: boolean) {
    setSelected((current) => (checked ? [...current, eventId] : current.filter((id) => id !== eventId)));
  }

  function reset() {
    setCampaign(null);
    setSelected([]);
    setTitle("");
    setSubject("");
    setBody("");
    create.reset();
    revise.reset();
    release.reset();
  }

  function saveDraft() {
    const content = { subject: subject.trim(), body: body.trim() };
    const onSuccess = (saved: Campaign) => {
      setCampaign(saved);
      setReviewed(content);
    };
    if (locked) {
      revise.mutate(content, { onSuccess });
      return;
    }
    create.mutate(
      {
        workspaceId,
        eventIds: selected,
        title: title.trim(),
        ...content,
        acknowledgementPolicy: acknowledgement,
        dueAt: schedule.dueAt?.toISOString(),
      },
      { onSuccess },
    );
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle>New communication</CardTitle>
        <CardDescription>
          Write one message for one or more events. Save the draft to preview it, then send it now or at the scheduled time.
        </CardDescription>
      </CardHeader>
      <CardContent>
        <form
          className="space-y-5"
          onSubmit={(event) => {
            event.preventDefault();
            if (canSave && (!locked || contentChanged)) saveDraft();
          }}
        >
          <EventPicker events={events} selected={selected} onToggle={toggleEvent} disabled={locked} more={moreEvents} />

          <DeliverySettings disabled={locked} acknowledgement={acknowledgement} onAcknowledgement={setAcknowledgement} />

          <ScheduleFields disabled={locked} schedule={schedule} selectedEvents={selectedEvents} />

          <div className="space-y-2">
            <Label htmlFor="comm-title">Title (organizers only)</Label>
            <Input id="comm-title" value={title} disabled={locked} onChange={(e) => setTitle(e.target.value)} />
          </div>
          <div className="space-y-2">
            <Label htmlFor="comm-subject">Subject</Label>
            <Input id="comm-subject" value={subject} onChange={(e) => setSubject(e.target.value)} />
          </div>
          <div className="space-y-2">
            <Label htmlFor="comm-body">Message (Markdown)</Label>
            <Textarea id="comm-body" rows={6} value={body} onChange={(e) => setBody(e.target.value)} />
          </div>

          {writeError ? (
            <Alert variant="destructive" role="alert">
              <AlertDescription>{writeError}</AlertDescription>
            </Alert>
          ) : null}

          <div className="flex flex-wrap gap-2">
            <Button type="submit" disabled={!canSave || create.isPending || revise.isPending || (locked && !contentChanged)}>
              {locked ? "Update preview" : "Save draft and preview"}
            </Button>
            {locked ? (
              <Button
                type="button"
                variant="outline"
                disabled={discard.isPending}
                onClick={() => discard.mutate(campaign.id, { onSuccess: reset })}
              >
                Discard draft
              </Button>
            ) : null}
          </div>
        </form>

        {campaign && revisionId ? (
          <div className="mt-6 space-y-4 border-t pt-6">
            <PreviewPanel preview={preview} />
            {contentChanged ? (
              <p className="text-sm text-muted-foreground">You changed the message. Update the preview before you send.</p>
            ) : null}
            <Button
              disabled={!previewed || contentChanged || release.isPending}
              onClick={() =>
                release.mutate(
                  { campaignId: campaign.id, revisionId },
                  { onSuccess: () => navigate(ctfAdminCommunicationPath(campaign.id)) },
                )
              }
            >
              {campaign.trigger.due_at ? "Schedule" : "Send now"}
            </Button>
          </div>
        ) : null}
      </CardContent>
    </Card>
  );
}
