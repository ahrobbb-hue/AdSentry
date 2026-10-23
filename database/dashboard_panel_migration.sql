-- Persistent AdSentry operations-center message configuration.
-- Run once in Supabase after the other AdSentry feature migrations.

CREATE TABLE IF NOT EXISTS public.staff_dashboard_panels (
    guild_id BIGINT PRIMARY KEY,
    channel_id BIGINT NOT NULL,
    message_id BIGINT,
    updated_by BIGINT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE public.staff_dashboard_panels ENABLE ROW LEVEL SECURITY;
