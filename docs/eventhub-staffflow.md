# EventHub and StaffFlow

Run `database/eventhub_staffflow_migration.sql` once in the Supabase SQL Editor before using these features. The bot must use `SUPABASE_SERVICE_ROLE_KEY`; the tables have RLS enabled and expose no client-side policies. Configure the private staff review channel with `/ad-setup` before using StaffFlow requests.

Staff can use `/dashboard` and open the EventHub or StaffFlow page to use graphical buttons and forms for the core actions below. The slash commands remain available as shortcuts.

## EventHub (`/event`)

- `/event create` publishes an event with an ISO 8601 start time, capacity (1–500), channel, and optional reminder lead time.
- The announcement has a persistent registration button. Registration is serialized in PostgreSQL, preventing duplicates and seat-limit overruns when many members click at once.
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
