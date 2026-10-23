-- Run in the Supabase SQL Editor to add a dedicated private StaffFlow review channel.
ALTER TABLE public.server_configs
    ADD COLUMN IF NOT EXISTS staffflow_review_channel_id BIGINT;
