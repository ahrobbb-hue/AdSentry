-- Separate registration availability from the event's lifecycle status.
-- Run after eventhub_event_duration_migration.sql.

ALTER TABLE public.events
    ADD COLUMN IF NOT EXISTS registration_open BOOLEAN NOT NULL DEFAULT TRUE;

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
    IF v_event.status <> 'OPEN' OR v_event.starts_at <= NOW() THEN
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

    IF v_count >= v_event.capacity THEN
        RETURN jsonb_build_object('result', 'full');
    END IF;

    INSERT INTO public.event_participants(event_id, guild_id, user_id, user_name)
    VALUES (p_event_id, p_guild_id, p_user_id, p_user_name);
    RETURN jsonb_build_object('result', 'registered');
END;
$$;
REVOKE ALL ON FUNCTION public.register_event_participant(BIGINT, BIGINT, BIGINT, TEXT)
    FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.register_event_participant(BIGINT, BIGINT, BIGINT, TEXT)
    TO service_role;

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
    IF v_event.status <> 'OPEN' OR v_event.starts_at <= NOW() THEN
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

    IF v_count < v_event.capacity THEN
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
REVOKE ALL ON FUNCTION public.register_event_participant_advanced(BIGINT, BIGINT, BIGINT, TEXT, JSONB)
    FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.register_event_participant_advanced(BIGINT, BIGINT, BIGINT, TEXT, JSONB)
    TO service_role;
