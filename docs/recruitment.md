# Staff recruitment

Recruitment is managed by authorized staff (server administrators/moderators or configured `STAFF_ROLE_IDS`); members can only submit applications.

1. Run `database/recruitment_migration.sql` in the Supabase SQL Editor.
2. Configure `SUPABASE_SERVICE_ROLE_KEY` for the bot. The migration enables row-level security without public policies because applications contain personal information.
3. Ensure the bot can send embeds in the public announcement and private staff review channels. Keep the review channel restricted to staff.
4. Use `/recruit create` and choose a role from the menu. Built-in options are Administrator, Moderator, Ticket Support, Management, Events Team, and Design Team. Selecting **Other** opens a field for a custom role name.
5. Add server-specific options with `/recruit role-add`; remove them with `/recruit role-remove`. The picker keeps built-in options and supports up to 18 custom options.
6. Applicants use **Apply now**. Each member can apply once per opening.
7. Staff use **Accept** or **Reject** on the private review card. The applicant is notified by DM when possible.
8. Use `/recruit close` with the opening ID shown on the announcement to stop new applications. `/recruit open` remains available for direct title entry.

Openings and applications are stored separately from advertisement submissions. Persistent buttons are restored after a bot restart. The SQL migration is required before the recruitment commands can be used.
