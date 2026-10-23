-- Recruitment feature migration for Supabase/PostgreSQL.
-- Run once in Supabase SQL Editor before using /recruit open.

CREATE TABLE IF NOT EXISTS public.recruitments (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    guild_id BIGINT NOT NULL,
    title TEXT NOT NULL CHECK (char_length(title) BETWEEN 1 AND 80),
    description TEXT NOT NULL CHECK (char_length(description) BETWEEN 1 AND 1800),
    created_by BIGINT NOT NULL,
    public_channel_id BIGINT NOT NULL,
    review_channel_id BIGINT NOT NULL,
    announcement_message_id BIGINT,
    status TEXT NOT NULL DEFAULT 'OPEN' CHECK (status IN ('OPEN', 'CLOSED')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    closed_at TIMESTAMPTZ
);

-- Enforce one active opening per role per server, including concurrent commands.
CREATE UNIQUE INDEX IF NOT EXISTS idx_recruitments_one_open_role
    ON public.recruitments (guild_id, lower(title)) WHERE status = 'OPEN';
CREATE INDEX IF NOT EXISTS idx_recruitments_guild_status
    ON public.recruitments (guild_id, status);

CREATE TABLE IF NOT EXISTS public.recruitment_applications (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    recruitment_id BIGINT NOT NULL REFERENCES public.recruitments(id) ON DELETE CASCADE,
    guild_id BIGINT NOT NULL,
    user_id BIGINT NOT NULL,
    user_name TEXT NOT NULL,
    experience TEXT NOT NULL CHECK (char_length(experience) BETWEEN 1 AND 900),
    motivation TEXT NOT NULL CHECK (char_length(motivation) BETWEEN 1 AND 900),
    availability TEXT NOT NULL CHECK (char_length(availability) BETWEEN 1 AND 300),
    status TEXT NOT NULL DEFAULT 'PENDING' CHECK (status IN ('PENDING', 'ACCEPTED', 'REJECTED')),
    reviewed_by BIGINT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    reviewed_at TIMESTAMPTZ,
    UNIQUE (recruitment_id, user_id)
);

CREATE INDEX IF NOT EXISTS idx_recruitment_applications_pending
    ON public.recruitment_applications (status, id) WHERE status = 'PENDING';
CREATE INDEX IF NOT EXISTS idx_recruitment_applications_user
    ON public.recruitment_applications (guild_id, user_id, created_at DESC);

-- These tables contain applicant data. The bot accesses them server-side with
-- the Supabase service-role key; expose no direct anon/authenticated policies.
ALTER TABLE public.recruitments ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.recruitment_applications ENABLE ROW LEVEL SECURITY;
