-- Adds scheduled closing, seat limits, interview stages, application questions,
-- and optional Discord role assignment to an existing recruitment installation.
-- Run once in Supabase SQL Editor after recruitment_migration.sql.

ALTER TABLE public.recruitments
    ADD COLUMN IF NOT EXISTS seat_limit INTEGER,
    ADD COLUMN IF NOT EXISTS closes_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS questions JSONB NOT NULL DEFAULT '[]'::jsonb,
    ADD COLUMN IF NOT EXISTS grant_role_id BIGINT;

ALTER TABLE public.recruitment_applications
    ADD COLUMN IF NOT EXISTS custom_answers JSONB NOT NULL DEFAULT '[]'::jsonb;

ALTER TABLE public.recruitment_applications
    DROP CONSTRAINT IF EXISTS recruitment_applications_status_check;
ALTER TABLE public.recruitment_applications
    ADD CONSTRAINT recruitment_applications_status_check
    CHECK (status IN ('PENDING', 'INTERVIEW', 'ACCEPTED', 'REJECTED'));

ALTER TABLE public.recruitments
    DROP CONSTRAINT IF EXISTS recruitments_seat_limit_check;
ALTER TABLE public.recruitments
    ADD CONSTRAINT recruitments_seat_limit_check
    CHECK (seat_limit IS NULL OR seat_limit BETWEEN 1 AND 100);

CREATE INDEX IF NOT EXISTS idx_recruitments_due
    ON public.recruitments (closes_at) WHERE status = 'OPEN' AND closes_at IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_recruitment_applications_capacity
    ON public.recruitment_applications (recruitment_id, status);

-- Serialize submissions against deadline closures and duplicate applications.
CREATE OR REPLACE FUNCTION public.submit_recruitment_application(
    p_recruitment_id BIGINT,
    p_guild_id BIGINT,
    p_user_id BIGINT,
    p_user_name TEXT,
    p_experience TEXT,
    p_motivation TEXT,
    p_availability TEXT,
    p_custom_answers JSONB DEFAULT '[]'::jsonb
) RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
    v_opening public.recruitments%ROWTYPE;
    v_application public.recruitment_applications%ROWTYPE;
BEGIN
    SELECT * INTO v_opening
    FROM public.recruitments
    WHERE id = p_recruitment_id AND guild_id = p_guild_id
    FOR UPDATE;
    IF NOT FOUND OR v_opening.status <> 'OPEN' THEN
        RETURN jsonb_build_object('created', FALSE, 'reason', 'closed');
    END IF;
    IF v_opening.closes_at IS NOT NULL AND v_opening.closes_at <= NOW() THEN
        UPDATE public.recruitments
        SET status = 'CLOSED', closed_at = COALESCE(closed_at, NOW())
        WHERE id = v_opening.id;
        RETURN jsonb_build_object('created', FALSE, 'reason', 'closed');
    END IF;

    BEGIN
        INSERT INTO public.recruitment_applications (
            recruitment_id, guild_id, user_id, user_name,
            experience, motivation, availability, custom_answers, status
        ) VALUES (
            p_recruitment_id, p_guild_id, p_user_id, p_user_name,
            p_experience, p_motivation, p_availability, COALESCE(p_custom_answers, '[]'::jsonb), 'PENDING'
        ) RETURNING * INTO v_application;
    EXCEPTION WHEN unique_violation THEN
        RETURN jsonb_build_object('created', FALSE, 'reason', 'duplicate');
    END;

    RETURN jsonb_build_object('created', TRUE, 'application', to_jsonb(v_application));
END;
$$;

-- Serialize final acceptances per opening so concurrent staff clicks cannot
-- exceed its seat limit. Intermediate interviews remain reversible.
CREATE OR REPLACE FUNCTION public.review_recruitment_application(
    p_application_id BIGINT,
    p_guild_id BIGINT,
    p_reviewer_id BIGINT,
    p_status TEXT
) RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
    v_application public.recruitment_applications%ROWTYPE;
    v_opening public.recruitments%ROWTYPE;
    v_recruitment_id BIGINT;
    v_accepted INTEGER;
    v_capacity_reached BOOLEAN := FALSE;
BEGIN
    IF p_status IS NULL OR p_status NOT IN ('INTERVIEW', 'ACCEPTED', 'REJECTED') THEN
        RAISE EXCEPTION 'Invalid recruitment decision';
    END IF;

    -- Lock the opening first, matching the submit procedure's lock order.
    -- This avoids deadlocks between a duplicate submit and a staff review.
    SELECT recruitment_id INTO v_recruitment_id
    FROM public.recruitment_applications
    WHERE id = p_application_id AND guild_id = p_guild_id
    ;
    IF NOT FOUND THEN
        RETURN jsonb_build_object('changed', FALSE, 'reason', 'not_found');
    END IF;

    SELECT * INTO v_opening
    FROM public.recruitments
    WHERE id = v_recruitment_id AND guild_id = p_guild_id
    FOR UPDATE;
    IF NOT FOUND THEN
        RETURN jsonb_build_object('changed', FALSE, 'reason', 'opening_not_found');
    END IF;

    SELECT * INTO v_application
    FROM public.recruitment_applications
    WHERE id = p_application_id AND guild_id = p_guild_id
    FOR UPDATE;
    IF NOT FOUND THEN
        RETURN jsonb_build_object('changed', FALSE, 'reason', 'not_found');
    END IF;
    IF v_application.status NOT IN ('PENDING', 'INTERVIEW') OR v_application.status = p_status THEN
        RETURN jsonb_build_object('changed', FALSE, 'reason', 'already_reviewed');
    END IF;

    IF p_status = 'ACCEPTED' AND v_opening.seat_limit IS NOT NULL THEN
        SELECT count(*) INTO v_accepted
        FROM public.recruitment_applications
        WHERE recruitment_id = v_opening.id AND status = 'ACCEPTED';
        IF v_accepted >= v_opening.seat_limit THEN
            RETURN jsonb_build_object('changed', FALSE, 'reason', 'capacity');
        END IF;
    END IF;

    UPDATE public.recruitment_applications
    SET status = p_status, reviewed_by = p_reviewer_id, reviewed_at = NOW()
    WHERE id = p_application_id;

    IF p_status = 'ACCEPTED' AND v_opening.seat_limit IS NOT NULL THEN
        SELECT count(*) INTO v_accepted
        FROM public.recruitment_applications
        WHERE recruitment_id = v_opening.id AND status = 'ACCEPTED';
        IF v_accepted >= v_opening.seat_limit THEN
            v_capacity_reached := TRUE;
            UPDATE public.recruitments
            SET status = 'CLOSED', closed_at = COALESCE(closed_at, NOW())
            WHERE id = v_opening.id AND status = 'OPEN';
        END IF;
    END IF;

    RETURN jsonb_build_object('changed', TRUE, 'capacity_reached', v_capacity_reached);
END;
$$;

REVOKE ALL ON FUNCTION public.review_recruitment_application(BIGINT, BIGINT, BIGINT, TEXT) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.review_recruitment_application(BIGINT, BIGINT, BIGINT, TEXT) TO service_role;
REVOKE ALL ON FUNCTION public.submit_recruitment_application(BIGINT, BIGINT, BIGINT, TEXT, TEXT, TEXT, TEXT, JSONB) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.submit_recruitment_application(BIGINT, BIGINT, BIGINT, TEXT, TEXT, TEXT, TEXT, JSONB) TO service_role;

ALTER TABLE public.recruitments ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.recruitment_applications ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.recruitment_role_options ENABLE ROW LEVEL SECURITY;
