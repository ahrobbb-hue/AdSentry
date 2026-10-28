-- ================================================================
-- EventHub Channels Migration
-- Adds admin_channel_id to events table for private staff notifications
-- Run this in your Supabase SQL Editor
-- ================================================================

-- Add admin notification channel column to events
ALTER TABLE public.events ADD COLUMN IF NOT EXISTS admin_channel_id BIGINT;

-- Backfill existing events: use the guild's staff_review_channel if available
-- (optional, can be left NULL for existing events)
-- UPDATE public.events e
-- SET admin_channel_id = s.staff_review_channel_id
-- FROM public.server_configs s
-- WHERE e.guild_id = s.guild_id AND e.admin_channel_id IS NULL;

-- Index for fast admin channel lookups
CREATE INDEX IF NOT EXISTS idx_events_admin_channel ON public.events(admin_channel_id) WHERE admin_channel_id IS NOT NULL;
