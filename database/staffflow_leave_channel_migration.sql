-- Run in the Supabase SQL Editor to add separate private StaffFlow destinations.
ALTER TABLE public.server_configs
    ADD COLUMN IF NOT EXISTS staffflow_review_channel_id BIGINT;

ALTER TABLE public.server_configs
    ADD COLUMN IF NOT EXISTS staffflow_complaint_channel_id BIGINT;

-- Refresh PostgREST's schema cache so the bot can see the new columns immediately.
NOTIFY pgrst, 'reload schema';
