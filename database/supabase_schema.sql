-- ================================================================
-- AdSentry - Supabase PostgreSQL Schema
-- Run this script in your Supabase SQL Editor: Dashboard > SQL Editor
-- ================================================================

-- 1. Server Configurations Table: Stores dynamic channel IDs configured via /ad-setup
CREATE TABLE IF NOT EXISTS public.server_configs (
    guild_id BIGINT PRIMARY KEY,
    announcement_channel_id BIGINT NOT NULL,
    staff_review_channel_id BIGINT NOT NULL,
    last_panel_message_id BIGINT,          -- Message ID of active submission panel
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Schema Migration (run if server_configs already exists):
ALTER TABLE public.server_configs ADD COLUMN IF NOT EXISTS last_panel_message_id BIGINT;

-- 2. Submissions Table: Stores ad applications from submission to review
CREATE TABLE IF NOT EXISTS public.submissions (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    guild_id BIGINT,                       -- Discord Server ID
    user_id BIGINT NOT NULL,
    user_name TEXT NOT NULL,
    user_avatar_url TEXT,
    category TEXT NOT NULL,
    title TEXT,
    content TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'PENDING',  -- 'PENDING', 'APPROVED', 'REJECTED'
    review_message_id BIGINT,              -- Discord message ID in staff review channel
    reviewed_by BIGINT,                   -- Moderator User ID
    rejection_reason TEXT,                 -- Reason sent to user upon rejection
    language TEXT NOT NULL DEFAULT 'en',   -- Submission language: 'ar' or 'en'
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    reviewed_at TIMESTAMPTZ
);

-- Schema Migration for language and image_url columns:
ALTER TABLE public.submissions ADD COLUMN IF NOT EXISTS language TEXT DEFAULT 'en';
ALTER TABLE public.submissions ADD COLUMN IF NOT EXISTS image_url TEXT;

-- 3. User Cooldowns Table: Tracks anti-spam and submission rate limits
CREATE TABLE IF NOT EXISTS public.user_cooldowns (
    user_id BIGINT PRIMARY KEY,
    last_submission_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    cooldown_until TIMESTAMPTZ NOT NULL
);

-- 4. Active Posts Table: Tracks published announcements for Auto-Expiry
CREATE TABLE IF NOT EXISTS public.active_posts (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    submission_id BIGINT NOT NULL REFERENCES public.submissions(id) ON DELETE CASCADE,
    guild_id BIGINT,
    channel_id BIGINT NOT NULL,
    message_id BIGINT NOT NULL,
    thread_id BIGINT,                      -- Discussion thread ID linked to the ad
    webhook_url TEXT,
    published_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMPTZ,                -- NULL = 'Forever'
    is_expired BOOLEAN NOT NULL DEFAULT FALSE
);

-- Indexes for lightning fast lookups
CREATE INDEX IF NOT EXISTS idx_server_configs_guild ON public.server_configs(guild_id);
CREATE INDEX IF NOT EXISTS idx_submissions_status ON public.submissions(status);
CREATE INDEX IF NOT EXISTS idx_submissions_user_id ON public.submissions(user_id);
CREATE INDEX IF NOT EXISTS idx_user_cooldowns_until ON public.user_cooldowns(cooldown_until);
CREATE INDEX IF NOT EXISTS idx_active_posts_expiry ON public.active_posts(expires_at, is_expired);

-- 5. Ad Interests Table: Tracks user bookmarks / interests for published ads (⭐ button)
CREATE TABLE IF NOT EXISTS public.ad_interests (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    post_id BIGINT NOT NULL REFERENCES public.active_posts(id) ON DELETE CASCADE,
    user_id BIGINT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(post_id, user_id)
);

CREATE INDEX IF NOT EXISTS idx_ad_interests_post ON public.ad_interests(post_id);
CREATE INDEX IF NOT EXISTS idx_ad_interests_user ON public.ad_interests(user_id);
