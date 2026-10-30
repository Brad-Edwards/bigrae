# Scoped CTF communications

The communication API lets an organizer author a message for participants in one
or more events with one account, organization or workspace parent. You need
current permission to manage communications on **every** target event.
Membership and administrator flags alone do not grant that permission.

## Author and release

Use `/api/v1/ctf/communications/` with an authenticated browser session or an API
token. The native credential ceiling and current policy must permit
`event.manage_communications` for every target. Recipient details also require
`event.manage_participants` on the selected event. Event participation does not
grant communication administration.

Create a campaign with exactly one parent UUID (`account`, `organization` or
`workspace`), `title`,
`target_event_ids`, `audience_spec`, `trigger_spec`, `channels`, `subject`, and
`body`. `acknowledgement_policy` defaults to `none`; `read` and `explicit` are
also supported. The generated OpenAPI contract describes closed selector and
trigger variants. Unknown fields and duplicate JSON keys are rejected.

| Operation | Route relative to `/api/v1/ctf/` |
| --- | --- |
| Create a draft | `POST communications/` |
| List campaigns | `GET communications/?workspace=<uuid>&limit=25&offset=0` |
| Read summary | `GET communications/<campaign_id>/` |
| Append draft content revision | `POST communications/<campaign_id>/revisions/` |
| Release or schedule | `POST communications/<campaign_id>/release/` |
| Cancel unclaimed work | `POST communications/<campaign_id>/cancel/` |
| List events and parents you can message | `GET communications/target-options/?workspace=<uuid>` |
| Read the workspace compatibility projection | `GET communications/targets/?workspace_id=<uuid>` |
| Preview a revision | `GET communications/<campaign_id>/preview/?revision_id=<uuid>` |
| Read delivery state | `GET communications/<campaign_id>/delivery/` |
| Read recipient state for one event | `GET communications/<campaign_id>/recipients/?intent_id=<uuid>&event_id=<uuid>` |
| Read the activity history | `GET communications/<campaign_id>/activity/` |

Each route requires current native authority and a live credential.
Browser sessions send the CSRF token on every POST.
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

1. Select the account, organization or workspace. A message reaches only events
   with that exact parent. Changing the selection clears the current draft.
2. Select one or more events. The list shows only events where you hold
   permission to manage communications.
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
The credential must permit the same actions and exact target scopes. If one
event is missing, has a different parent or is not yours to message, the full
request fails with one generic error. The platform saves nothing from a failed create or release. After
release, delivery to one recipient can fail while other deliveries succeed. The
campaign page shows each result.

| Current authority | Available projection |
| --- | --- |
| Manage communications on every campaign event | Compose, preview, delivery counts and activity |
| The same authority plus manage participants on the selected event | Recipient rows for that event |
| Membership, legacy administrator flags or participation alone | No organizer communication access |

Service-principal actions retain their own identity in the activity history.
They are not attributed to the human responsible for the service.

**Cancel unclaimed work** stops delivery commands that no worker has claimed,
and stops a scheduled release. A message already in the inbox, or accepted by a
transport, stays delivered.

Changing event times keeps a scheduled communication at its own due time.

## Participant inbox

Open **Participate > Inbox** (`/ctf/inbox/`). The inbox shows the messages for
your active event only.

1. Select a message to open it. The page records a read when the message opens.
   If the page cannot record the read, select **Try again**.
2. If the message shows **Needs acknowledgement**, select **Acknowledge**.
3. To hide a message, select **Dismiss**. Dismiss does not mark the message read
   or acknowledged.
4. Use **Needs action** to find messages that still need a read or an
   acknowledgement. Use **Dismissed** to find hidden messages.

A message that still needs an action stays in **Inbox**, also after you dismiss
it. A notice above each workspace page shows the number of these messages. The
notice does not block other pages, help, the range, or the terminal.

The page refreshes when you return to the tab, when the network returns, and
every 60 seconds. A socket notice only starts a refresh. It never marks a
message read. Other open tabs refresh after an action. When the browser is
offline, the page shows that the messages can be out of date, and actions are
not available.

Messages use a safe Markdown format. Links open only same-site paths or the
HTTPS hosts that the deployment allows. Other links show as plain text. Images,
raw HTML and embedded media are not accepted.

### Inbox API

The API is session-only. It is scoped to the parent event:
`/api/v1/ctf/me/events/<event_id>/communications/`.

| Operation | Route | Effect |
| --- | --- | --- |
| List | `GET ?view=inbox\|action_required\|dismissed&limit=&offset=` | No receipt change. Adds `unread_count`, `action_required_count` and `allowed_link_hosts`. |
| Read one message | `GET <message_id>/` | No receipt change. |
| Read | `POST <message_id>/read/` | Sets `read_at` once. |
| Dismiss | `POST <message_id>/dismiss/` | Sets `dismissed_at` once. |
| Acknowledge | `POST <message_id>/acknowledge/` | Sets `acknowledged_at` once. Needs the pinned `explicit` policy. |

POST an empty object. Browser writes need CSRF. A repeated action keeps the
first timestamp. No action changes another action's timestamp. Each item shows
`action_required` while its pinned policy needs a read or an acknowledgement.
Responses are private and the browser does not store them. An invalid request
returns a generic error that does not repeat the input.

Banned or removed participants cannot read messages or change receipts. A
disqualified participant who can view the event can manage their own receipts.
Temporary accounts must satisfy their live participation and password-change
requirements. Email-only declarations do not appear in the inbox. A message is
not available after the retention window, also before the purge job deletes it.

Retained legacy notification bodies are not replayed through the CTF
notification socket.

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
not silently changed to in-app. The legacy provisioning-failure and capacity
email alerts stay unavailable. The organizer alerts below replace the automatic
start and end alerts.

## Organizer alerts

The platform alerts the event owner and each co-organizer about automatic
event changes. Moderators and judges do not get these alerts.

| Alert | When |
| --- | --- |
| Event started automatically | The event starts at its scheduled start time. |
| Event could not start automatically | The start time passes, but the event content is not ready. The platform tries again with backoff. |
| Participant ranges are not ready | At the start time, fewer participant ranges are ready than registered participants. The alert gives the counts. Provisioning continues. |
| Event ended automatically | The event ends at its scheduled end time. This includes a paused event. |

Open **Monitoring > Alerts** for the event. The tab shows the unread count.

1. Read the alert text.
2. Select **Mark as read** to record a read.
3. Select **Dismiss** to hide the alert. Select **Show dismissed** to see it again.

The platform also sends each alert by email when the deployment has a mail
provider. The email links to the event's **Alerts** tab. Range alerts need permission to manage ranges on the event. A person
who stops being the owner or a co-organizer loses the alerts at once. Pending
email to that person stops.

Submissions close at the exact event end time.
