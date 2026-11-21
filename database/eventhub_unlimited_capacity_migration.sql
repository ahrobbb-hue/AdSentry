-- Use capacity 0 to represent an unlimited number of event participants.
-- Run after eventhub_registration_control_migration.sql and
-- eventhub_manual_role_slots_migration.sql.

ALTER TABLE public.events
    DROP CONSTRAINT IF EXISTS events_capacity_check;
ALTER TABLE public.events
    ADD CONSTRAINT events_capacity_check CHECK (capacity BETWEEN 0 AND 500);

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
    IF p_capacity IS NULL OR p_capacity NOT BETWEEN 0 AND 500 THEN
        RAISE EXCEPTION 'Event capacity must be 0 (unlimited) or between 1 and 500';
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
       OR p_capacity IS NULL OR p_capacity NOT BETWEEN 0 AND 500
       OR p_starts_at IS NULL OR p_starts_at <= NOW()
       OR p_ends_at IS NULL OR p_ends_at < p_starts_at + INTERVAL '1 minute'
       OR p_ends_at > p_starts_at + INTERVAL '28 days' THEN
        RAISE EXCEPTION 'Invalid event title, description, capacity or schedule';
    END IF;

    SELECT count(*) INTO v_registered
    FROM public.event_participants
    WHERE event_id = p_event_id AND status = 'REGISTERED';

    IF p_capacity <> 0 AND p_capacity < v_registered THEN
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

CREATE OR REPLACE FUNCTION public.register_event_participant(
    p_event_id BIGINT,
    p_guild_id BIGINT,
    p_user_id BIGINT,
    p_user_name TEXT
) RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
    v_event public.events%ROWTYPE;
    v_count INTEGER;
BEGIN
    SELECT * INTO v_event
    FROM public.events
    WHERE id = p_event_id AND guild_id = p_guild_id
    FOR UPDATE;

    IF NOT FOUND THEN
        RETURN jsonb_build_object('result', 'not_found');
    END IF;
    IF v_event.status <> 'OPEN'
       OR COALESCE(v_event.ends_at, v_event.starts_at) <= NOW() THEN
        RETURN jsonb_build_object('result', 'closed');
    END IF;
    IF NOT v_event.registration_open THEN
        RETURN jsonb_build_object('result', 'registration_closed');
    END IF;
    IF EXISTS (
        SELECT 1 FROM public.event_participants
        WHERE event_id = p_event_id AND user_id = p_user_id
    ) THEN
        RETURN jsonb_build_object('result', 'duplicate');
    END IF;

    SELECT count(*) INTO v_count
    FROM public.event_participants
    WHERE event_id = p_event_id;

    IF v_event.capacity <> 0 AND v_count >= v_event.capacity THEN
        RETURN jsonb_build_object('result', 'full');
    END IF;

    INSERT INTO public.event_participants(event_id, guild_id, user_id, user_name)
    VALUES (p_event_id, p_guild_id, p_user_id, p_user_name);
    RETURN jsonb_build_object('result', 'registered');
END;
$$;

CREATE OR REPLACE FUNCTION public.register_event_participant_advanced(
    p_event_id BIGINT,
    p_guild_id BIGINT,
    p_user_id BIGINT,
    p_user_name TEXT,
    p_form_answers JSONB DEFAULT '{}'::jsonb
) RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
    v_event public.events%ROWTYPE;
    v_count INTEGER;
    v_waitlist_count INTEGER;
    v_suspension public.event_suspensions%ROWTYPE;
    v_status TEXT;
    v_pos INTEGER := NULL;
    v_participant_id BIGINT;
BEGIN
    SELECT * INTO v_suspension
    FROM public.event_suspensions
    WHERE guild_id = p_guild_id
      AND user_id = p_user_id
      AND suspended_until > NOW();
    IF FOUND THEN
        RETURN jsonb_build_object(
            'result', 'suspended',
            'suspended_until', v_suspension.suspended_until,
            'reason', v_suspension.reason
        );
    END IF;

    SELECT * INTO v_event
    FROM public.events
    WHERE id = p_event_id AND guild_id = p_guild_id
    FOR UPDATE;

    IF NOT FOUND THEN
        RETURN jsonb_build_object('result', 'not_found');
    END IF;
    IF v_event.status <> 'OPEN'
       OR COALESCE(v_event.ends_at, v_event.starts_at) <= NOW() THEN
        RETURN jsonb_build_object('result', 'closed');
    END IF;
    IF NOT v_event.registration_open THEN
        RETURN jsonb_build_object('result', 'registration_closed');
    END IF;

    IF EXISTS (
        SELECT 1 FROM public.event_participants
        WHERE event_id = p_event_id
          AND user_id = p_user_id
          AND status IN ('REGISTERED', 'WAITLIST', 'PENDING')
    ) THEN
        RETURN jsonb_build_object('result', 'duplicate');
    END IF;

    IF v_event.registration_type = 'FORM' THEN
        INSERT INTO public.event_participants(
            event_id, guild_id, user_id, user_name, status, form_answers
        ) VALUES (
            p_event_id, p_guild_id, p_user_id, p_user_name, 'PENDING', p_form_answers
        ) RETURNING id INTO v_participant_id;

        RETURN jsonb_build_object(
            'result', 'pending_approval',
            'participant_id', v_participant_id
        );
    END IF;

    SELECT count(*) INTO v_count
    FROM public.event_participants
    WHERE event_id = p_event_id AND status = 'REGISTERED';

    IF v_event.capacity = 0 OR v_count < v_event.capacity THEN
        v_status := 'REGISTERED';
    ELSIF v_event.enable_waitlist THEN
        SELECT count(*) INTO v_waitlist_count
        FROM public.event_participants
        WHERE event_id = p_event_id AND status = 'WAITLIST';
        v_pos := v_waitlist_count + 1;
        v_status := 'WAITLIST';
    ELSE
        RETURN jsonb_build_object('result', 'full');
    END IF;

    INSERT INTO public.event_participants(
        event_id, guild_id, user_id, user_name, status, waitlist_position, form_answers
    ) VALUES (
        p_event_id, p_guild_id, p_user_id, p_user_name, v_status, v_pos, p_form_answers
    ) RETURNING id INTO v_participant_id;

    RETURN jsonb_build_object(
        'result', lower(v_status),
        'position', v_pos,
        'participant_id', v_participant_id
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

REVOKE ALL ON FUNCTION public.update_event_details(
    BIGINT, BIGINT, TEXT, TEXT, INTEGER, TIMESTAMPTZ, TIMESTAMPTZ
) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.update_event_details(
    BIGINT, BIGINT, TEXT, TEXT, INTEGER, TIMESTAMPTZ, TIMESTAMPTZ
) TO service_role;

REVOKE ALL ON FUNCTION public.register_event_participant(BIGINT, BIGINT, BIGINT, TEXT)
    FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.register_event_participant(BIGINT, BIGINT, BIGINT, TEXT)
    TO service_role;

REVOKE ALL ON FUNCTION public.register_event_participant_advanced(BIGINT, BIGINT, BIGINT, TEXT, JSONB)
    FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.register_event_participant_advanced(BIGINT, BIGINT, BIGINT, TEXT, JSONB)
    TO service_role;

NOTIFY pgrst, 'reload schema';
