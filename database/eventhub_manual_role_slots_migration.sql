-- Let staff choose an available participant role for each new event.
-- Run after eventhub_event_duration_migration.sql.

DROP FUNCTION IF EXISTS public.create_event_with_slot(
    BIGINT, BIGINT, TEXT, TEXT, BIGINT, TIMESTAMPTZ, TIMESTAMPTZ, INTEGER,
    TIMESTAMPTZ, TEXT, JSONB, TEXT, BIGINT, BIGINT[]
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
    p_role_ids BIGINT[],
    p_role_slot INTEGER
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
       OR p_ends_at IS NULL OR p_ends_at < p_starts_at + INTERVAL '1 minute'
       OR p_ends_at > p_starts_at + INTERVAL '28 days' THEN
        RAISE EXCEPTION 'Event start and end times are invalid';
    END IF;
    IF p_capacity NOT BETWEEN 1 AND 500 THEN
        RAISE EXCEPTION 'Event capacity must be between 1 and 500';
    END IF;
    IF p_role_slot IS NULL OR p_role_slot NOT BETWEEN 1 AND 5 THEN
        RETURN jsonb_build_object('created', FALSE, 'reason', 'invalid_slot');
    END IF;
    IF p_role_ids IS NULL OR cardinality(p_role_ids) <> 5 THEN
        RAISE EXCEPTION 'Exactly five event role IDs are required';
    END IF;

    PERFORM pg_advisory_xact_lock(p_guild_id);

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

    IF (
        SELECT count(*) FROM public.events
        WHERE guild_id = p_guild_id AND status = 'OPEN'
    ) >= 5 THEN
        RETURN jsonb_build_object('created', FALSE, 'reason', 'limit');
    END IF;

    IF EXISTS (
        SELECT 1 FROM public.events
        WHERE guild_id = p_guild_id AND status = 'OPEN' AND role_slot = p_role_slot
    ) THEN
        RETURN jsonb_build_object('created', FALSE, 'reason', 'slot_taken');
    END IF;

    INSERT INTO public.events (
        guild_id, created_by, title, description, channel_id, starts_at, ends_at,
        capacity, status, reminder_at, registration_type, form_questions,
        prizes, admin_channel_id, enable_waitlist, role_slot, role_id
    ) VALUES (
        p_guild_id, p_created_by, p_title, p_description, p_channel_id, p_starts_at, p_ends_at,
        p_capacity, 'OPEN', p_reminder_at, COALESCE(p_registration_type, 'DIRECT'),
        COALESCE(p_form_questions, '[]'::jsonb), p_prizes, p_admin_channel_id,
        TRUE, p_role_slot, p_role_ids[p_role_slot]
    )
    RETURNING * INTO v_event;

    RETURN jsonb_build_object(
        'created', TRUE,
        'role_slot', p_role_slot,
        'event', to_jsonb(v_event)
    );
END;
$$;

REVOKE ALL ON FUNCTION public.create_event_with_slot(
    BIGINT, BIGINT, TEXT, TEXT, BIGINT, TIMESTAMPTZ, TIMESTAMPTZ, INTEGER,
    TIMESTAMPTZ, TEXT, JSONB, TEXT, BIGINT, BIGINT[], INTEGER
) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.create_event_with_slot(
    BIGINT, BIGINT, TEXT, TEXT, BIGINT, TIMESTAMPTZ, TIMESTAMPTZ, INTEGER,
    TIMESTAMPTZ, TEXT, JSONB, TEXT, BIGINT, BIGINT[], INTEGER
) TO service_role;

NOTIFY pgrst, 'reload schema';
