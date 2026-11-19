# AdSentry

AdSentry is a bilingual Discord bot for submitting, reviewing, moderating, and
publishing server advertisements. It also includes recruitment, EventHub,
StaffFlow, and staff-dashboard features. Supabase is the primary database.

## Requirements

- Python 3.10 or later
- A Discord application and bot token
- A Supabase project

## Install and configure

In PowerShell, from the project directory:

```powershell
py -3.10 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
```

Edit `.env` and set `DISCORD_TOKEN`, `SUPABASE_URL`, and
`SUPABASE_SERVICE_ROLE_KEY`. Keep the service-role key private and never commit
`.env`. `GEMINI_API_KEY` is optional; translation falls back to
`deep-translator`. Set `STAFF_ROLE_IDS` to comma-separated staff role IDs if
needed.

Enable the **Server Members Intent** and **Message Content Intent** in the
Discord Developer Portal. Invite the bot with the application-command scope
and permissions needed for the enabled features, including sending messages,
embeds, threads, and managing webhooks.

## Database setup

Run `database/supabase_schema.sql` in the Supabase SQL Editor first. Then apply
the migration files required by the features you plan to use. The EventHub and
StaffFlow setup is described in [docs/eventhub-staffflow.md](docs/eventhub-staffflow.md);
recruitment setup is described in [docs/recruitment.md](docs/recruitment.md).
For moderation statistics, audit logs, and reusable reply templates, apply
`database/moderation_experience_migration.sql`.
For anonymous reviewer ratings and the `/reviewer-stats` command, also apply
`database/enhancements_migration.sql`. Server owners and members with Manage
Server can view per-reviewer star distributions; voter identities are not shown.
The bot does not automatically apply database migrations.

After configuring the database, start the bot:

```powershell
python main.py
```

Run `/ad-setup` in each server to configure the announcement and staff-review
channels. Use `/dashboard` to create the staff operations panel where needed.
Set `GUILD_ID` in `.env` to synchronize commands to one development server;
leave it unset to synchronize commands globally.

## Checks

Run the test suite with:

```powershell
python -m pytest
```
