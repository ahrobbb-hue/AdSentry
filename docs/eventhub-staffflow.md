# EventHub and StaffFlow

Run `database/eventhub_staffflow_migration.sql` and then `database/eventhub_advanced_migration.sql` in the Supabase SQL Editor before using the advanced EventHub features. Apply `database/eventhub_contest_slots_migration.sql` after them to enable up to five open events/contests per server and separate participant roles. The bot must use `SUPABASE_SERVICE_ROLE_KEY`; the tables have RLS enabled and expose no client-side policies. Configure the private staff review channel with `/ad-setup` before using StaffFlow requests.

Staff can use `/dashboard` and open the EventHub, StaffFlow, or dedicated Shifts page to use graphical buttons and forms for the core actions below. The shifts page shows the 10 nearest scheduled shifts and lets administrators schedule a new shift. The slash commands remain available as shortcuts.

## EventHub (`/event`)

- `/event create` publishes an event with an ISO 8601 start time, capacity (1–500), channel, and optional reminder lead time.
- A server can have up to five open events/contests at once. Each receives its own participant role: `Event`, `Event 2`, `Event 3`, `Event 4`, or `Event 5`. A slot and its role are reused after an event is closed.
- The announcement has persistent registration and cancellation buttons. Successful registrations and waitlist promotions receive only that event's role; pending applications and waitlisted members do not receive it until accepted or promoted. Closing an event removes its role from registered participants.
- Registration is serialized in PostgreSQL, preventing duplicates and seat-limit overruns when many members click at once. Concurrent event creation is also serialized, so the five-event limit cannot be exceeded by simultaneous staff actions.
- The bot needs Discord's **Manage Roles** permission, and its highest role must be above the five `Event` roles.
- `/event list` shows open events and current occupancy.
- `/event attendance` records or corrects attendance for a registered member; staff only.
- `/event close` closes registration; staff only. Reminders are sent automatically once when due.

Use a timezone offset in the ISO date, for example `2026-11-01T19:00+03:00`. A naive ISO date is interpreted as UTC.

## StaffFlow (`/staffflow`)

- `/staffflow leave` submits a dated leave request.
- `/staffflow complaint` submits a private administrative complaint.
- Both are delivered to the configured staff review channel and are visible to people with access to that channel.
- `/staffflow review` approves or rejects a pending case, records the reviewer and note, updates the review card, and attempts to notify the requester by DM.
- `/staffflow shift-create` schedules a member's shift and sends a DM; `/staffflow shift-list` displays upcoming assignments. Staff can inspect a member's schedule, while members can inspect their own.
- `/staffflow promote` records a position change and reason in the audit table and posts it to the review channel.

Staff checks follow the bot's existing `is_staff` rule: configured `STAFF_ROLE_IDS` or Discord Administrator, Manage Messages, or Manage Server permission.

## Data and access

Event participant data, staff cases, schedules, and audit records are server-side operational data. Keep the service role key secret and limit access to the staff review channel. `review_staff_case` and `register_event_participant` are security-definer database operations with execution restricted to `service_role`.
