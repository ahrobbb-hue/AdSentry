-- ================================================================
-- AdSentry Enhancements Migration
-- Features: Tags, Templates, Ratings, Change Requests, Expiry Alerts, Pinned Ads
-- ================================================================

-- 1. Add Tags System to Submissions
ALTER TABLE public.submissions ADD COLUMN IF NOT EXISTS tags TEXT[];
CREATE INDEX IF NOT EXISTS idx_submissions_tags ON public.submissions USING GIN(tags);

-- 2. Add Templates System
CREATE TABLE IF NOT EXISTS public.ad_templates (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id BIGINT NOT NULL,
    guild_id BIGINT NOT NULL,
    name TEXT NOT NULL,                    -- Template name (user defined)
    category TEXT NOT NULL,
    title TEXT,
    content TEXT NOT NULL,
    tags TEXT[],
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(user_id, guild_id, name)
);

CREATE INDEX IF NOT EXISTS idx_templates_user ON public.ad_templates(user_id, guild_id);

-- 3. Add Review Ratings System
CREATE TABLE IF NOT EXISTS public.review_ratings (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    submission_id BIGINT NOT NULL REFERENCES public.submissions(id) ON DELETE CASCADE,
    guild_id BIGINT NOT NULL,
    user_id BIGINT NOT NULL,               -- Submitter who rates
    reviewer_id BIGINT NOT NULL,           -- Staff who reviewed
    rating INT NOT NULL CHECK (rating BETWEEN 1 AND 5),
    feedback TEXT,                         -- Optional feedback text
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(submission_id)
);

CREATE INDEX IF NOT EXISTS idx_ratings_reviewer ON public.review_ratings(reviewer_id);
CREATE INDEX IF NOT EXISTS idx_ratings_guild ON public.review_ratings(guild_id);

-- 4. Add Change Request System to Submissions
ALTER TABLE public.submissions ADD COLUMN IF NOT EXISTS change_requests INT DEFAULT 0;
ALTER TABLE public.submissions ADD COLUMN IF NOT EXISTS max_change_requests INT DEFAULT 3;
ALTER TABLE public.submissions ADD COLUMN IF NOT EXISTS last_change_note TEXT;
ALTER TABLE public.submissions ADD COLUMN IF NOT EXISTS change_requested_by BIGINT;
ALTER TABLE public.submissions ADD COLUMN IF NOT EXISTS change_requested_at TIMESTAMPTZ;

-- 5. Add Expiry Notification System
ALTER TABLE public.active_posts ADD COLUMN IF NOT EXISTS expiry_alert_sent BOOLEAN DEFAULT FALSE;
ALTER TABLE public.active_posts ADD COLUMN IF NOT EXISTS removed_at TIMESTAMPTZ;

-- 6. Add Pinned Ads System
ALTER TABLE public.active_posts ADD COLUMN IF NOT EXISTS is_pinned BOOLEAN DEFAULT FALSE;
ALTER TABLE public.active_posts ADD COLUMN IF NOT EXISTS pinned_at TIMESTAMPTZ;
ALTER TABLE public.active_posts ADD COLUMN IF NOT EXISTS pinned_by BIGINT;
ALTER TABLE public.active_posts ADD COLUMN IF NOT EXISTS pin_expires_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_active_posts_pinned ON public.active_posts(is_pinned, pinned_at) WHERE is_pinned = TRUE;

-- 7. Change Request History Table (optional, for audit trail)
CREATE TABLE IF NOT EXISTS public.submission_change_history (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    submission_id BIGINT NOT NULL REFERENCES public.submissions(id) ON DELETE CASCADE,
    request_number INT NOT NULL,           -- 1st, 2nd, or 3rd request
    requested_by BIGINT NOT NULL,          -- Staff member ID
    note TEXT NOT NULL,                    -- What needs to be changed
    old_content TEXT,                      -- Snapshot before edit
    new_content TEXT,                      -- Content after user edit
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ                 -- When user submitted the edit
);

CREATE INDEX IF NOT EXISTS idx_change_history_submission ON public.submission_change_history(submission_id);

-- ================================================================
-- VERIFICATION QUERIES (Run these to confirm migration success)
-- ================================================================

-- Check submissions has new columns:
-- SELECT column_name FROM information_schema.columns WHERE table_name = 'submissions' AND column_name IN ('tags', 'change_requests', 'max_change_requests');

-- Check new tables exist:
-- SELECT table_name FROM information_schema.tables WHERE table_name IN ('ad_templates', 'review_ratings', 'submission_change_history');

-- Check active_posts has new columns:
-- SELECT column_name FROM information_schema.columns WHERE table_name = 'active_posts' AND column_name IN ('is_pinned', 'expiry_alert_sent');
