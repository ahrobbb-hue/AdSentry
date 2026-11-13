# EventHub and StaffFlow

Run `database/eventhub_staffflow_migration.sql` and then `database/eventhub_advanced_migration.sql` in the Supabase SQL Editor before using the advanced EventHub features. Apply `database/eventhub_contest_slots_migration.sql` after them to strictly limit each server to five open events, assign separate participant roles, and support event editing/cancellation. Then apply `database/eventhub_event_duration_migration.sql`, `database/eventhub_manual_role_slots_migration.sql`, and `database/eventhub_registration_control_migration.sql` in order to add explicit end times, staff-selected participant roles, and separate registration state from event state. If the duration migration was already applied, rerun it to update the maximum duration to four weeks; it safely replaces its functions. The slot migration also closes any pre-existing overflow, keeping the five soonest upcoming events. The bot must use `SUPABASE_SERVICE_ROLE_KEY`; the tables have RLS enabled and expose no client-side policies. Configure the private staff review channel with `/ad-setup` before using StaffFlow requests.

Staff can use `/dashboard` and open the EventHub, StaffFlow, or dedicated Shifts page to use graphical buttons and forms for the core actions below. The shifts page shows the 10 nearest scheduled shifts and lets administrators schedule a new shift. The slash commands remain available as shortcuts.

## EventHub (`/event`)

- `/event create` publishes an event with an ISO 8601 start time, duration (from `1m` up to `4w`; defaults to `2h`), capacity (1–500), channel, and optional reminder lead time. Duration accepts `m` minutes, `h` hours, `d` days, or `w` weeks; a number without a unit is still interpreted as hours.
- A server can have up to five open events/contests at once. Each receives its own participant role: `Event`, `Event 2`, `Event 3`, `Event 4`, or `Event 5`. A slot and its role are reused after an event is closed.
- When creating an event, staff select which of the five roles it uses. Roles already assigned to another open event are not offered, and the database rejects a role claimed concurrently by another event creation.
- `/setup` creates or refreshes a private EventHub category with five role-specific text rooms (`event-1` through `event-5`) and one shared voice room. Only administrators can see all rooms; each Event role can see its own text room and the shared voice room. The bot needs **Manage Channels** and **Manage Roles** to configure them.
- The announcement has persistent registration and cancellation buttons. Successful registrations and waitlist promotions receive only that event's role; pending applications and waitlisted members do not receive it until accepted or promoted. Closing an event removes its role from registered participants.
- Registration is serialized in PostgreSQL, preventing duplicates and seat-limit overruns when many members click at once. Concurrent event creation is also serialized, so the five-event limit cannot be exceeded by simultaneous staff actions.
- The bot needs Discord's **Manage Roles** permission, and its highest role must be above the five `Event` roles.
- `/event list` shows open events and current occupancy.
- From `/dashboard` → **الفعاليات**, staff can choose an open event with **تعديل فعالية** or **إلغاء فعالية** buttons; these remain available even when no separate admin notification channel was configured.
- The event creator or staff can use `/event edit event_id:<id>` or the **تعديل الفعالية** button to change the title, description, start time, duration (such as `30m`, `1h`, `1d`, or `2w`; up to `4w`), or capacity. Enter the new start time as ISO 8601 with an offset, e.g. `2026-11-18T19:00+03:00`. Capacity cannot be reduced below the number already registered; changes update the public announcement and reset its start reminder.
- The event creator or staff can use `/event cancel event_id:<id>` or **إلغاء الفعالية**. Cancellation removes participant roles, marks the existing announcement as cancelled, and posts a cancellation notice in the same announcement channel. The optional cancellation reason is included in that notice.
- `/event attendance` records or corrects attendance for a registered member; staff only.
- **إغلاق التسجيل** in the dashboard, the admin card, or `/event close event_id:<id>` only prevents new registrations. The event stays open, current participant roles remain, and it closes automatically at its configured end time; roles are removed then. Use the separate **إلغاء الفعالية** action to cancel an event before its end.
- Existing events receive a one-time two-hour end time when the duration migration is applied. Reminders are sent automatically once when due.

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
