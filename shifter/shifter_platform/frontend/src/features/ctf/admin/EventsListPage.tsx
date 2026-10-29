import { useState } from "react";

import { Link } from "react-router";

import { Plus } from "lucide-react";

import { useCtfEvents } from "@/api/ctfAdmin";
import { PageHeader } from "@/components/page-header";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button, buttonVariants } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Skeleton } from "@/components/ui/skeleton";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { cn } from "@/lib/utils";

import { formatDateTime, titleCase } from "../format";
import { ctfAdminEventCreatePath, ctfAdminEventPath } from "../routes";

function EventsListBody({ query }: Readonly<{ query: ReturnType<typeof useCtfEvents> }>) {
  if (query.isLoading) {
    return (
      <div className="space-y-3 p-4">
        {[0, 1, 2, 3].map((row) => (
          <Skeleton key={row} className="h-10 w-full" />
        ))}
      </div>
    );
  }

  if (query.isError) {
    return (
      <div className="p-4">
        <Alert variant="destructive">
          <AlertTitle>Could not load events</AlertTitle>
          <AlertDescription>Please retry.</AlertDescription>
        </Alert>
      </div>
    );
  }

  const events = query.data?.events ?? [];
  if (events.length === 0) {
    return (
      <div className="grid place-items-center px-6 py-16 text-center">
        <p className="text-sm font-medium">No events yet</p>
        <p className="mt-1 text-sm text-muted-foreground">Create the first event to get started.</p>
      </div>
    );
  }

  return (
    <Table>
      <TableHeader>
        <TableRow className="hover:bg-transparent">
          <TableHead>Name</TableHead>
          <TableHead>Owner</TableHead>
          <TableHead className="w-[140px]">Status</TableHead>
          <TableHead className="w-[90px]">Mode</TableHead>
          <TableHead className="w-[190px]">Starts</TableHead>
          <TableHead className="w-[190px]">Ends</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {events.map((event) => (
          <TableRow key={event.id}>
            <TableCell className="font-medium">
              <Link className="hover:underline" to={ctfAdminEventPath(event.id)}>
                {event.name}
              </Link>
            </TableCell>
            <TableCell className="text-sm text-muted-foreground">
              {event.owner.display_name}
              {event.access_source === "platform_admin" ? (
                <Badge variant="outline" className="ml-2">
                  Admin
                </Badge>
              ) : null}
            </TableCell>
            <TableCell>
              <Badge variant="secondary">{titleCase(event.status)}</Badge>
            </TableCell>
            <TableCell className="text-sm text-muted-foreground">{event.team_mode ? "Team" : "Solo"}</TableCell>
            <TableCell className="text-sm text-muted-foreground">{formatDateTime(event.event_start)}</TableCell>
            <TableCell className="text-sm text-muted-foreground">{formatDateTime(event.event_end)}</TableCell>
          </TableRow>
        ))}
      </TableBody>
    </Table>
  );
}

export function EventsListPage() {
  const [page, setPage] = useState(1);
  const [search, setSearch] = useState("");
  const query = useCtfEvents(page, search);
  const count = query.data?.count ?? 0;
  const countNoun = count === 1 ? "event" : "events";
  const description = query.data ? `${count} ${countNoun}` : "CTF events";

  return (
    <>
      <PageHeader
        title="Events"
        description={description}
        actions={
          <Link to={ctfAdminEventCreatePath()} className={cn(buttonVariants({ size: "sm" }))}>
            <Plus className="size-4" />
            New event
          </Link>
        }
      />
      <div className="space-y-2">
        <Label htmlFor="event-search">Search events</Label>
        <Input id="event-search" maxLength={100} value={search} onChange={event => { setSearch(event.target.value); setPage(1); }} />
      </div>
      <Card className="overflow-hidden py-0" aria-busy={query.isFetching}>
        <EventsListBody query={query} />
      </Card>
      <nav aria-label="Event pages" className="flex items-center gap-3">
        <Button variant="outline" disabled={page === 1 || query.isFetching} onClick={() => setPage(current => current - 1)}>Previous events</Button>
        <span>Page {page}</span>
        <Button variant="outline" disabled={!query.data?.next_page || query.isFetching} onClick={() => setPage(query.data?.next_page ?? page)}>Next events</Button>
      </nav>
    </>
  );
}
