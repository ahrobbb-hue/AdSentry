# Staff recruitment

## Interactive dashboard

Staff can run `/dashboard channel:#operations-room` once to post the permanent operations-center panel in a private administration channel. Its buttons keep working after bot restarts, and the bot edits or refreshes this same message instead of deleting it or creating a new one. Clicking a system opens a private graphical page for the staff member while leaving the permanent panel untouched. Running `/dashboard` later returns the configured channel.

From the recruitment page, staff can create or close openings. Use the permanent panel's **قنوات مراجعة التقديم** button to select up to three private review channels. The create flow asks which configured channel receives the opening's applicant cards, then opens the role and application form. The public announcement uses the channel configured through `/ad-setup`.

Run `database/dashboard_panel_migration.sql` once in Supabase to store the permanent panel. Also run `database/recruitment_dashboard_migration.sql` for channel selection and `database/eventhub_staffflow_migration.sql` if the EventHub and StaffFlow tables are not installed yet. Keep the operations and review channels private to their intended teams.

Recruitment is managed by authorized staff (server administrators/moderators or configured `STAFF_ROLE_IDS`); members can only submit applications.

1. Run `database/recruitment_migration.sql`, then `database/recruitment_enhancements_migration.sql` in the Supabase SQL Editor.
2. Configure `SUPABASE_SERVICE_ROLE_KEY` for the bot. The migrations enable row-level security without public policies because applications contain personal information.
3. Ensure the bot can send embeds in the public announcement and private staff review channels. Keep the review channel restricted to staff.
4. Use `/recruit create` and choose a role from the menu. Built-in options are Administrator, Moderator, Ticket Support, Management, Events Team, and Design Team. Selecting **Other** opens a field for a custom role name.
5. The setup form lets staff set the seat count, optional close time (1–720 hours), and up to two extra application questions. The bot closes the opening automatically at its deadline and when the final seat is accepted.
6. Add server-specific role options with `/recruit role-add`; remove them with `/recruit role-remove`. The picker supports up to 18 custom options.
7. Optionally select a Discord role in `/recruit create` to grant it when an applicant is accepted. The bot requires Manage Roles, and its highest role must be above the selected role. Without this option, acceptance does not change member roles.
8. Applicants use **Apply now**. Each member can apply once per opening. Staff can invite applicants to interview, then accept or reject them from the private review card.
9. Use `/recruit close` with the opening ID shown on the announcement to stop new applications. `/recruit open` remains available for direct title entry.

Openings and applications are stored separately from advertisement submissions. Persistent buttons are restored after a bot restart. The SQL migration is required before the recruitment commands can be used.
