# Staff recruitment

Recruitment is managed by authorized staff (server administrators/moderators or configured `STAFF_ROLE_IDS`); members can only submit applications.

1. Run `database/recruitment_migration.sql` in the Supabase SQL Editor.
2. Configure `SUPABASE_SERVICE_ROLE_KEY` for the bot. The migration enables row-level security without public policies because applications contain personal information.
3. Ensure the bot can send embeds in the public announcement and private staff review channels. Keep the review channel restricted to staff.
4. Use `/recruit open` to publish a role, description, public channel, and review channel.
5. Applicants use **Apply now**. Each member can apply once per opening.
6. Staff use **Accept** or **Reject** on the private review card. The applicant is notified by DM when possible.
7. Use `/recruit close` with the opening ID shown on the announcement to stop new applications.

Openings and applications are stored separately from advertisement submissions. Persistent buttons are restored after a bot restart. The SQL migration is required before the recruitment commands can be used.
