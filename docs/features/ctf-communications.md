# Scoped CTF communications

The communication API lets an organizer author a message for participants in one
or more events within one workspace. The actor must have live notification
permission on **every** target event and communication access to that workspace.
Workspace membership alone does not grant event authority.

## Author and release

Use `/api/v1/ctf/communications/` with an authenticated browser session or an API
token. Tokens need the exact `ctf:communication:read` or
`ctf:communication:write` scope for the requested operation; neither scope
implies the other. Existing event/play scopes do not grant communication access.

Create a campaign with `workspace_id` (the public workspace UUID), `title`,
`target_event_ids`, `audience_spec`, `trigger_spec`, `channels`, `subject`, and
`body`. `acknowledgement_policy` defaults to `none`; `read` and `explicit` are
also supported. The generated OpenAPI contract describes closed selector and
trigger variants. Unknown fields and duplicate JSON keys are rejected.

| Operation | Route relative to `/api/v1/ctf/` |
| --- | --- |
| Create a draft | `POST communications/` |
| List campaigns | `GET communications/?workspace_id=<uuid>&limit=25&offset=0` |
| Read summary | `GET communications/<campaign_id>/` |
| Append draft content revision | `POST communications/<campaign_id>/revisions/` |
| Release or schedule | `POST communications/<campaign_id>/release/` |
| Cancel unclaimed work | `POST communications/<campaign_id>/cancel/` |
| List events you can message | `GET communications/targets/?workspace_id=<uuid>` |
| Preview a revision | `GET communications/<campaign_id>/preview/?revision_id=<uuid>` |
| Read delivery state | `GET communications/<campaign_id>/delivery/` |
| Read recipient state for one event | `GET communications/<campaign_id>/recipients/?intent_id=<uuid>&event_id=<uuid>` |
| Read the activity history | `GET communications/<campaign_id>/activity/` |

GET routes need `ctf:communication:read`. POST routes need
`ctf:communication:write`. Browser sessions send the CSRF token on every POST.
GET routes change no state.

Release requires an `occurrence_key`; reuse the same key when retrying the same
request. A future absolute-time trigger creates a scheduled declaration. The
scheduler rechecks the original actor and token before releasing it. Revoking a
token or changing its owner prevents subsequent admission, including replay.

HTTP 202 means the ledger accepted the declaration. It does not mean an email
was delivered, a socket received a message, or a participant read it. The in-app
channel is available. The email channel is available only when the deployment
configures a mail provider. A console, file, or dummy backend makes email
unavailable. A release that selects an unavailable channel returns
dependency-unavailable and creates no queued work. Cancellation cannot recall an
accepted message.

The worker sends each email once per attempt and retries transient failures with
backoff. It stops at the attempt and elapsed-time limits. **Accepted** means the
mail provider took the message. It does not prove mailbox delivery. A worker
crash after the provider accepts a message can send a duplicate.

Collections default to 25 campaigns and are capped at 100 per request. Campaigns
are visible only when the actor may access every target. Visibility is applied
before pagination; `next_offset` advances through authorized campaigns only.
The organizer page can load subsequent pages to inspect or cancel older work.
Bodies are limited to 65,536 UTF-8 bytes, targets to 100, and explicit audience
lists to 5,000 references. Authoring and release share admission rate budgets.

## Multi-event console

Open **CTF operations > Communications** (`/ctf/admin/communications/`). The
event **Notifications** tab also links to it.

1. Select the workspace. A message reaches only events in one workspace.
2. Select one or more events. The list shows only events where you hold
   notification permission.
3. Write the title, subject, and Markdown message. Select the acknowledgement.
4. Select **Send now**, or select **Schedule** and enter a date, time, and time
   zone. The console shows the UTC time and the local time of each event. It
   refuses a time that a clock change skips or repeats.
5. Select **Save draft and preview**. The preview shows the message and the
   current recipient count for each event. Preview sends nothing.
6. To change the text, edit it and select **Update preview**. Each change makes
   a new revision. The events, timing, channel, and acknowledgement stay fixed.
   To change them, discard the draft and start again.
7. Select **Send now** or **Schedule**. The button is available only after the
   preview loads, and only when the audience and channels are within limits.
   The console sends the revision that you previewed. If the request fails, select the button again. The retry uses the
   same campaign and occurrence.

Recipient counts in the preview are estimates. The platform resolves recipients
when it releases the message. A scheduled message resolves them at its due time.

### Delivery state and history

The campaign page shows each occurrence with recipients, inbox availability,
read, and acknowledged counts for each event. It shows delivery-command counts
for each channel. **Accepted** means a transport took the message. Read and
acknowledged come only from participant actions. A failed release task shows an
alert, because the message was not delivered.

Each occurrence also shows one delivery state:

| State | Meaning |
| --- | --- |
| `pending` | Some delivery work is queued, in progress, or waiting to retry. |
| `accepted` | Every recipient channel accepted the message. |
| `partial` | Some recipient channels accepted it, and some did not. |
| `failed` | No recipient channel accepted it, and at least one failed. |
| `withdrawn` | Cancellation or a lifecycle change stopped all delivery work. |
| `empty` | The release found no recipients. |
| `not_released` | The occurrence is not released yet. |

The page refreshes every 15 seconds while delivery work or a scheduled release
is pending. **Show recipients** lists the participant name, team, delivery status, and
receipt times for one event. It needs participant permission on that event. It
never shows an email address. The activity history lists who created, revised,
sent, scheduled, or cancelled the message, and when. It contains no message text.

### Authorization and failure

The platform checks your permission on every selected event for every request.
It also checks your workspace membership. If one event is missing, in another
workspace, or not yours to message, the full request fails with one generic
error. The platform saves nothing from a failed create or release. After
release, delivery to one recipient can fail while other deliveries succeed. The
campaign page shows each result.

| Role on the event | Compose and read | Recipient rows |
| --- | --- | --- |
| Owner or co-organizer | Yes | Yes |
| Moderator | Yes | Yes |
| Judge | No | No |
| Platform administrator who is a workspace member | Yes | Yes |
| Participant or removed staff | No | No |

**Cancel unclaimed work** stops delivery commands that no worker has claimed,
and stops a scheduled release. A message already in the inbox, or accepted by a
transport, stays delivered.

Changing event times keeps a scheduled communication at its own due time.

## Participant inbox

The inbox is session-only and is scoped to the parent event:
`/api/v1/ctf/me/events/<event_id>/communications/`.
GET the collection or `<message_id>/` to view content without changing a receipt.
POST an empty object to `<message_id>/read/` or `<message_id>/acknowledge/` for a
deliberate action. Browser writes require CSRF. Repeating an action preserves its
first timestamp; acknowledgement requires the message's pinned `explicit` policy.

Banned or removed participants cannot fetch messages or change receipts. A
participant eligible to view an event may interact with their own receipts even
if disqualified from competition. Temporary accounts must satisfy their live
participation and password-change requirements. Email-only declarations do not
appear in the inbox.

WebSocket notifications are hints to fetch the durable inbox. Reconnect and
polling recover committed messages; socket writes never mark them read. Retained
legacy notification bodies are not replayed through the CTF notification socket.

## Legacy migration

Legacy notification create, send, schedule-cancel, bulk invitation and individual
login-information resend operations return HTTP 410. Use communication campaigns
for non-secret notices and the existing participant password operation for
explicit password issuance. Existing broad event tokens are not upgraded.

The organizer announcement dialog now writes to the participant inbox through
the communication API. Its retry preserves the campaign and occurrence identity.
Historical notification rows remain read-only; their dispatch counts are legacy
aggregate evidence, never delivery or read receipts.

Deployment uses the [maintenance cutover procedure](../technical/shifter_platform/ctf-communications.md#maintenance-cutover).
Migrated email work stays visibly unavailable while email is unavailable. It is
not silently changed to in-app. Organizer-only legacy email alerts are also
unavailable, because the participant ledger has no organizer recipients.
