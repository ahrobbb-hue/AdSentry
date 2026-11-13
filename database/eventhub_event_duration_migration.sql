-- Add explicit end times so participant roles can be removed when events finish.
-- Run after eventhub_contest_slots_migration.sql.

ALTER TABLE public.events
    ADD COLUMN IF NOT EXISTS ends_at TIMESTAMPTZ;

UPDATE public.events
SET ends_at = starts_at + INTERVAL '2 hours'
WHERE ends_at IS NULL;

ALTER TABLE public.events
    ALTER COLUMN ends_at SET NOT NULL;

ALTER TABLE public.events
    DROP CONSTRAINT IF EXISTS events_end_after_start_check;
ALTER TABLE public.events
    ADD CONSTRAINT events_end_after_start_check CHECK (ends_at > starts_at);

CREATE INDEX IF NOT EXISTS idx_events_open_ends_at
    ON public.events(ends_at)
    WHERE status = 'OPEN';

DROP FUNCTION IF EXISTS public.create_event_with_slot(
    BIGINT, BIGINT, TEXT, TEXT, BIGINT, TIMESTAMPTZ, INTEGER, TIMESTAMPTZ,
    TEXT, JSONB, TEXT, BIGINT, BIGINT[]
);

CREATE OR REPLACE FUNCTION public.create_event_with_slot(
    p_guild_id BIGINT,
    p_created_by BIGINT,
    p_title TEXT,
    p_description TEXT,
    p_channel_id BIGINT,
    p_starts_at TIMESTAMPTZ,
    p_ends_at TIMESTAMPTZ,
    p_capacity INTEGER,
    p_reminder_at TIMESTAMPTZ,
    p_registration_type TEXT,
    p_form_questions JSONB,
    p_prizes TEXT,
    p_admin_channel_id BIGINT,
    p_role_ids BIGINT[]
) RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
    v_slot INTEGER;
    v_candidate INTEGER;
    v_open_event RECORD;
    v_event public.events%ROWTYPE;
BEGIN
    IF p_starts_at IS NULL OR p_starts_at <= NOW()
       OR p_ends_at IS NULL OR p_ends_at <= p_starts_at
       OR p_ends_at > p_starts_at + INTERVAL '168 hours' THEN
        RAISE EXCEPTION 'Event start and end times are invalid';
    END IF;
    IF p_capacity NOT BETWEEN 1 AND 500 THEN
        RAISE EXCEPTION 'Event capacity must be between 1 and 500';
    END IF;

    PERFORM pg_advisory_xact_lock(p_guild_id);

    IF p_role_ids IS NULL OR cardinality(p_role_ids) <> 5 THEN
        RAISE EXCEPTION 'Exactly five event role IDs are required';
    END IF;

    FOR v_open_event IN
        SELECT id FROM public.events
        WHERE guild_id = p_guild_id AND status = 'OPEN' AND role_slot IS NULL
        ORDER BY created_at, id
    LOOP
        v_slot := NULL;
        FOR v_candidate IN 1..5 LOOP
            IF NOT EXISTS (
                SELECT 1 FROM public.events
                WHERE guild_id = p_guild_id AND status = 'OPEN' AND role_slot = v_candidate
            ) THEN
                v_slot := v_candidate;
                EXIT;
            END IF;
        END LOOP;
        EXIT WHEN v_slot IS NULL;
        UPDATE public.events
        SET role_slot = v_slot, role_id = p_role_ids[v_slot]
        WHERE id = v_open_event.id;
    END LOOP;

    IF (SELECT count(*) FROM public.events WHERE guild_id = p_guild_id AND status = 'OPEN') >= 5 THEN
        RETURN jsonb_build_object('created', FALSE, 'reason', 'limit');
    END IF;

    v_slot := NULL;
    FOR v_slot IN 1..5 LOOP
        IF NOT EXISTS (
            SELECT 1 FROM public.events
            WHERE guild_id = p_guild_id AND status = 'OPEN' AND role_slot = v_slot
        ) THEN
            EXIT;
        END IF;
    END LOOP;

    IF v_slot IS NULL OR v_slot > 5 THEN
        RETURN jsonb_build_object('created', FALSE, 'reason', 'limit');
    END IF;

    INSERT INTO public.events (
        guild_id, created_by, title, description, channel_id, starts_at, ends_at,
        capacity, status, reminder_at, registration_type, form_questions,
        prizes, admin_channel_id, enable_waitlist, role_slot, role_id
    ) VALUES (
        p_guild_id, p_created_by, p_title, p_description, p_channel_id, p_starts_at, p_ends_at,
        p_capacity, 'OPEN', p_reminder_at, COALESCE(p_registration_type, 'DIRECT'),
        COALESCE(p_form_questions, '[]'::jsonb), p_prizes, p_admin_channel_id,
        TRUE, v_slot, p_role_ids[v_slot]
    )
    RETURNING * INTO v_event;

    RETURN jsonb_build_object(
        'created', TRUE,
        'role_slot', v_slot,
        'event', to_jsonb(v_event)
    );
END;
$$;

REVOKE ALL ON FUNCTION public.create_event_with_slot(
    BIGINT, BIGINT, TEXT, TEXT, BIGINT, TIMESTAMPTZ, TIMESTAMPTZ, INTEGER,
    TIMESTAMPTZ, TEXT, JSONB, TEXT, BIGINT, BIGINT[]
) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.create_event_with_slot(
    BIGINT, BIGINT, TEXT, TEXT, BIGINT, TIMESTAMPTZ, TIMESTAMPTZ, INTEGER,
    TIMESTAMPTZ, TEXT, JSONB, TEXT, BIGINT, BIGINT[]
) TO service_role;

DROP FUNCTION IF EXISTS public.update_event_details(
    BIGINT, BIGINT, TEXT, TEXT, INTEGER, TIMESTAMPTZ
);

CREATE OR REPLACE FUNCTION public.update_event_details(
    p_event_id BIGINT,
    p_guild_id BIGINT,
    p_title TEXT,
    p_description TEXT,
    p_capacity INTEGER,
    p_starts_at TIMESTAMPTZ,
    p_ends_at TIMESTAMPTZ
) RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
    v_event public.events%ROWTYPE;
    v_registered INTEGER;
BEGIN
    SELECT * INTO v_event
    FROM public.events
    WHERE id = p_event_id AND guild_id = p_guild_id
    FOR UPDATE;

    IF NOT FOUND THEN
        RETURN jsonb_build_object('updated', FALSE, 'reason', 'not_found');
    END IF;
    IF v_event.status <> 'OPEN' THEN
        RETURN jsonb_build_object('updated', FALSE, 'reason', 'closed');
    END IF;
    IF p_title IS NULL OR char_length(btrim(p_title)) NOT BETWEEN 1 AND 100
       OR p_description IS NULL OR char_length(btrim(p_description)) NOT BETWEEN 1 AND 1800
       OR p_capacity NOT BETWEEN 1 AND 500
       OR p_starts_at IS NULL OR p_starts_at <= NOW()
       OR p_ends_at IS NULL OR p_ends_at <= p_starts_at
       OR p_ends_at > p_starts_at + INTERVAL '168 hours' THEN
        RAISE EXCEPTION 'Invalid event title, description, capacity or schedule';
    END IF;

    SELECT count(*) INTO v_registered
    FROM public.event_participants
    WHERE event_id = p_event_id AND status = 'REGISTERED';

    IF p_capacity < v_registered THEN
        RETURN jsonb_build_object(
            'updated', FALSE,
            'reason', 'capacity',
            'registered_count', v_registered
        );
    END IF;

    UPDATE public.events
    SET title = btrim(p_title),
        description = btrim(p_description),
        capacity = p_capacity,
        starts_at = p_starts_at,
        ends_at = p_ends_at,
        reminder_at = p_starts_at - INTERVAL '24 hours',
        reminded_at = NULL
    WHERE id = p_event_id
    RETURNING * INTO v_event;

    RETURN jsonb_build_object('updated', TRUE, 'event', to_jsonb(v_event));
END;
$$;
REVOKE ALL ON FUNCTION public.update_event_details(
    BIGINT, BIGINT, TEXT, TEXT, INTEGER, TIMESTAMPTZ, TIMESTAMPTZ
) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.update_event_details(
    BIGINT, BIGINT, TEXT, TEXT, INTEGER, TIMESTAMPTZ, TIMESTAMPTZ
) TO service_role;
