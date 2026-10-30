import { useState } from "react";

import { COMMUNICATION_PROFILE, type InboxItem } from "@/api/ctfInbox";
import {
  useCtfOrganizerAlertAction,
  useCtfOrganizerAlerts,
  type OrganizerAlertView,
} from "@/api/ctfOrganizerAlerts";
import { describeMutationError } from "@/api/errors";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";

import { formatDateTime } from "../format";
import { MarkdownContent } from "../MarkdownContent";

function AlertEntry({
  eventId,
  alert,
  allowedLinkHosts,
}: Readonly<{ eventId: string; alert: InboxItem; allowedLinkHosts: readonly string[] }>) {
  const action = useCtfOrganizerAlertAction(eventId);
  return (
    <li className="space-y-2 border-b pb-3 last:border-b-0 last:pb-0">
      <div className="flex flex-wrap items-center gap-2">
        <h3 className="text-sm font-medium">{alert.subject}</h3>
        {alert.read_at ? null : <Badge variant="default">Unread</Badge>}
        <span className="text-xs text-muted-foreground">
          {alert.created_at ? formatDateTime(alert.created_at) : null}
        </span>
      </div>
      {alert.content_profile === COMMUNICATION_PROFILE ? (
        <div className="text-sm">
          <MarkdownContent text={alert.body} profile={COMMUNICATION_PROFILE} allowedLinkHosts={allowedLinkHosts} />
        </div>
      ) : null}
      <div className="flex gap-2">
        {alert.read_at ? null : (
          <Button
            type="button"
            size="sm"
            variant="outline"
            disabled={action.isPending}
            onClick={() => action.mutate({ messageId: alert.message_id, action: "read" })}
          >
            Mark as read
          </Button>
        )}
        {alert.dismissed_at ? null : (
          <Button
            type="button"
            size="sm"
            variant="ghost"
            disabled={action.isPending}
            onClick={() => action.mutate({ messageId: alert.message_id, action: "dismiss" })}
          >
            Dismiss
          </Button>
        )}
      </div>
      {action.error ? (
        <p className="text-xs text-destructive">{describeMutationError(action.error, "Could not update the alert.")}</p>
      ) : null}
    </li>
  );
}

/** System alerts for the event owner and co-organizers (#528). */
export function OrganizerAlertsCard({ eventId }: Readonly<{ eventId: string }>) {
  const [view, setView] = useState<OrganizerAlertView>("inbox");
  const query = useCtfOrganizerAlerts(eventId, view);

  if (query.isLoading) return <Skeleton className="h-40 w-full" />;
  const alerts = query.data?.results ?? [];
  return (
    <Card>
      <CardContent className="space-y-4">
        <div className="flex flex-wrap items-start justify-between gap-2">
          <div>
            <h2 className="text-sm font-semibold">Organizer alerts</h2>
            <p className="mt-1 text-xs text-muted-foreground">
              Automatic start and end, a start that could not run, and ranges not ready at start.
            </p>
          </div>
          <div className="flex items-center gap-2">
            {query.data ? <Badge variant="outline">{query.data.unread_count} unread</Badge> : null}
            <Button
              type="button"
              size="sm"
              variant="outline"
              onClick={() => setView(view === "inbox" ? "dismissed" : "inbox")}
            >
              {view === "inbox" ? "Show dismissed" : "Show alerts"}
            </Button>
          </div>
        </div>
        {query.isError ? <p className="text-xs text-destructive">Alerts are unavailable.</p> : null}
        {!query.isError && alerts.length === 0 ? <p className="text-xs text-muted-foreground">No alerts.</p> : null}
        {alerts.length ? (
          <ul className="space-y-3">
            {alerts.map((alert) => (
              <AlertEntry
                key={alert.message_id}
                eventId={eventId}
                alert={alert}
                allowedLinkHosts={query.data?.allowed_link_hosts ?? []}
              />
            ))}
          </ul>
        ) : null}
      </CardContent>
    </Card>
  );
}
