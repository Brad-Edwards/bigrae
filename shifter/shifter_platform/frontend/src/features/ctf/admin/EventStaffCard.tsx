import { useState } from "react";

import {
  useAssignCtfEventStaff,
  useCtfEventStaff,
  useRevokeCtfEventStaff,
  useTransferCtfEventOwnership,
} from "@/api/ctfAdmin";
import { describeMutationError } from "@/api/errors";
import type { CtfEventStaffAssignRequest } from "@/api/types";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";

/**
 * Delegated staff management (CTF-607, #1922): moderators, judges, and full
 * co-organizers by principal UUID. `canManage` is a native policy hint; each
 * command is authorized again by its owning service. Pending changes can be
 * retried exactly, and ownership can transfer to a current co-organizer.
 */
export function EventStaffCard({ eventId, canManage = true }: Readonly<{ eventId: string; canManage?: boolean }>) {
  const staff = useCtfEventStaff(eventId, canManage);
  const assign = useAssignCtfEventStaff(eventId);
  const revoke = useRevokeCtfEventStaff(eventId);
  const transfer = useTransferCtfEventOwnership(eventId);
  const [principalUuid, setPrincipalUuid] = useState("");
  const [role, setRole] = useState<CtfEventStaffAssignRequest["role"]>("moderator");

  const assignError = describeMutationError(assign.error, "Could not assign staff.");
  const revokeError = describeMutationError(revoke.error, "Could not remove staff.");
  const transferError = describeMutationError(transfer.error, "Could not transfer ownership.");
  const pending = staff.data?.pending_transition;
  const changing = assign.isPending || revoke.isPending || transfer.isPending;

  if (!canManage) {
    return null;
  }

  return (
    <Card>
      <CardContent className="space-y-4">
        <div>
          <h2 className="text-sm font-semibold">Event staff</h2>
          <p className="mt-1 text-xs text-muted-foreground">
            Moderators manage participants and announcements; judges review submissions and grant awards; co-organizers
            hold every operational capability the owner has (configuration, challenges, participants, lifecycle,
            deletion) but cannot manage staff or transfer ownership. Assignments may name active human or service principals.
          </p>
        </div>
        {pending ? (
          <div role="status" className="space-y-2 rounded border border-border p-3 text-sm">
            <p>A staff change for <span className="font-mono">{pending.target_principal_uuid}</span> is awaiting confirmation.</p>
            <p className="text-xs text-muted-foreground">Retry the recorded change before starting another staff update.</p>
            <Button type="button" disabled={changing} onClick={() => {
              if (pending.kind === "transfer") transfer.mutate(pending.target_principal_uuid);
              else if (pending.kind === "revoke") revoke.mutate(pending.target_principal_uuid);
              else assign.mutate({ principal_uuid: pending.target_principal_uuid, role: pending.requested_role });
            }}>Retry pending change</Button>
            <Button type="button" variant="ghost" disabled={staff.isFetching} onClick={() => void staff.refetch()}>Check status</Button>
          </div>
        ) : null}
        <form
          className="flex flex-wrap items-end gap-2"
          onSubmit={(event) => {
            event.preventDefault();
            if (principalUuid.trim()) {
              assign.mutate(
                { principal_uuid: principalUuid.trim(), role },
                { onSuccess: () => setPrincipalUuid("") },
              );
            }
          }}
        >
          <div className="flex min-w-56 flex-1 flex-col gap-1">
            <Label htmlFor="staff-principal">Principal UUID</Label>
            <Input
              id="staff-principal"
              value={principalUuid}
              onChange={(event) => setPrincipalUuid(event.target.value)}
            />
          </div>
          <div className="flex flex-col gap-1">
            <Label htmlFor="staff-role">Role</Label>
            <Select value={role} onValueChange={(value) => setRole(value as CtfEventStaffAssignRequest["role"])}>
              <SelectTrigger id="staff-role" className="w-40">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="moderator">Moderator</SelectItem>
                <SelectItem value="judge">Judge</SelectItem>
                <SelectItem value="co_organizer">Co-organizer</SelectItem>
              </SelectContent>
            </Select>
          </div>
          <Button type="submit" disabled={!principalUuid.trim() || changing || Boolean(pending)}>
            Add staff
          </Button>
        </form>
        {assign.error ? <p className="text-xs text-destructive">{assignError}</p> : null}
        {revoke.error ? <p className="text-xs text-destructive">{revokeError}</p> : null}
        {transfer.error ? <p className="text-xs text-destructive">{transferError}</p> : null}
        {staff.data?.staff?.length ? (
          <ul className="divide-y divide-border/60">
            {staff.data.staff.map((member) => (
              <li key={member.principal_uuid} className="flex items-center justify-between gap-3 py-2 text-sm">
                <span>
                  <span className="font-mono">{member.principal_uuid}</span>{" "}
                  <span className="text-muted-foreground">· {member.role}</span>
                </span>
                <div className="flex items-center gap-1">
                  {member.role === "co_organizer" ? (
                    <Button
                      type="button"
                      size="sm"
                      variant="ghost"
                      disabled={changing || Boolean(pending)}
                      onClick={() => transfer.mutate(member.principal_uuid)}
                    >
                      Make owner
                    </Button>
                  ) : null}
                  <Button
                    type="button"
                    size="sm"
                    variant="ghost"
                    disabled={changing || Boolean(pending)}
                    onClick={() => revoke.mutate(member.principal_uuid)}
                  >
                    Remove
                  </Button>
                </div>
              </li>
            ))}
          </ul>
        ) : (
          <p className="text-xs text-muted-foreground">No staff assigned.</p>
        )}
      </CardContent>
    </Card>
  );
}
