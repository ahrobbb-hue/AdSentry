-- Allow recruitment creators and staff to update or cancel active openings.
-- Run after recruitment_migration.sql and recruitment_enhancements_migration.sql.

ALTER TABLE public.recruitments
    DROP CONSTRAINT IF EXISTS recruitments_status_check;
ALTER TABLE public.recruitments
    ADD CONSTRAINT recruitments_status_check
    CHECK (status IN ('OPEN', 'CLOSED', 'CANCELLED'));

CREATE OR REPLACE FUNCTION public.update_recruitment_details(
    p_recruitment_id BIGINT,
    p_guild_id BIGINT,
    p_title TEXT,
    p_description TEXT,
    p_seat_limit INTEGER,
    p_closes_at TIMESTAMPTZ
) RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
    v_opening public.recruitments%ROWTYPE;
    v_accepted INTEGER;
BEGIN
    SELECT * INTO v_opening
    FROM public.recruitments
    WHERE id = p_recruitment_id AND guild_id = p_guild_id
    FOR UPDATE;

    IF NOT FOUND THEN
        RETURN jsonb_build_object('updated', FALSE, 'reason', 'not_found');
    END IF;
    IF v_opening.status <> 'OPEN' THEN
        RETURN jsonb_build_object('updated', FALSE, 'reason', 'closed');
    END IF;
    IF p_title IS NULL OR char_length(btrim(p_title)) NOT BETWEEN 1 AND 80
       OR p_description IS NULL OR char_length(btrim(p_description)) NOT BETWEEN 1 AND 1800
       OR (p_seat_limit IS NOT NULL AND p_seat_limit NOT BETWEEN 1 AND 100)
       OR (p_closes_at IS NOT NULL AND p_closes_at <= NOW()) THEN
        RAISE EXCEPTION 'Invalid recruitment title, description, seat limit or deadline';
    END IF;

    IF EXISTS (
        SELECT 1 FROM public.recruitments
        WHERE guild_id = p_guild_id AND status = 'OPEN'
          AND id <> p_recruitment_id AND lower(title) = lower(btrim(p_title))
    ) THEN
        RETURN jsonb_build_object('updated', FALSE, 'reason', 'duplicate_title');
    END IF;

    SELECT count(*) INTO v_accepted
    FROM public.recruitment_applications
    WHERE recruitment_id = p_recruitment_id AND status = 'ACCEPTED';

    IF p_seat_limit IS NOT NULL AND p_seat_limit < v_accepted THEN
        RETURN jsonb_build_object(
            'updated', FALSE,
            'reason', 'capacity',
            'accepted_count', v_accepted
        );
    END IF;

    UPDATE public.recruitments
    SET title = btrim(p_title),
        description = btrim(p_description),
        seat_limit = p_seat_limit,
        closes_at = p_closes_at
    WHERE id = p_recruitment_id
    RETURNING * INTO v_opening;

    RETURN jsonb_build_object('updated', TRUE, 'opening', to_jsonb(v_opening));
END;
$$;
REVOKE ALL ON FUNCTION public.update_recruitment_details(BIGINT, BIGINT, TEXT, TEXT, INTEGER, TIMESTAMPTZ)
    FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.update_recruitment_details(BIGINT, BIGINT, TEXT, TEXT, INTEGER, TIMESTAMPTZ)
    TO service_role;

CREATE OR REPLACE FUNCTION public.cancel_recruitment(
    p_recruitment_id BIGINT,
    p_guild_id BIGINT
) RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
    v_opening public.recruitments%ROWTYPE;
BEGIN
    SELECT * INTO v_opening
    FROM public.recruitments
    WHERE id = p_recruitment_id AND guild_id = p_guild_id
    FOR UPDATE;

    IF NOT FOUND THEN
        RETURN jsonb_build_object('cancelled', FALSE, 'reason', 'not_found');
    END IF;
    IF v_opening.status <> 'OPEN' THEN
        RETURN jsonb_build_object('cancelled', FALSE, 'reason', 'closed');
    END IF;

    UPDATE public.recruitments
    SET status = 'CANCELLED', closed_at = NOW()
    WHERE id = p_recruitment_id
    RETURNING * INTO v_opening;

    RETURN jsonb_build_object('cancelled', TRUE, 'opening', to_jsonb(v_opening));
END;
$$;
REVOKE ALL ON FUNCTION public.cancel_recruitment(BIGINT, BIGINT) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.cancel_recruitment(BIGINT, BIGINT) TO service_role;

-- A cancelled opening must not accept further staff decisions on applications.
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

    SELECT recruitment_id INTO v_recruitment_id
    FROM public.recruitment_applications
    WHERE id = p_application_id AND guild_id = p_guild_id;
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
    IF v_opening.status = 'CANCELLED' THEN
        RETURN jsonb_build_object('changed', FALSE, 'reason', 'cancelled');
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
REVOKE ALL ON FUNCTION public.review_recruitment_application(BIGINT, BIGINT, BIGINT, TEXT)
    FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.review_recruitment_application(BIGINT, BIGINT, BIGINT, TEXT)
    TO service_role;
