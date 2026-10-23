# Staff recruitment

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
