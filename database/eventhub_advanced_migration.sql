-- ================================================================
-- AdSentry EventHub Advanced Migration
-- Comprehensive Events System with:
-- 1. Custom Location & Admin Channels
-- 2. Direct RSVP vs Custom Application Forms
-- 3. Smart Waitlists & Auto-Promotion
-- 4. Unified Participant Role support
-- 5. No-Show Strikes & Temporary Suspensions
-- ================================================================

-- 1. Server Configs enhancements for Events
ALTER TABLE public.server_configs ADD COLUMN IF NOT EXISTS events_admin_channel_id BIGINT;
ALTER TABLE public.server_configs ADD COLUMN IF NOT EXISTS events_role_id BIGINT;

-- 2. Events table enhancements
ALTER TABLE public.events ADD COLUMN IF NOT EXISTS admin_channel_id BIGINT;
ALTER TABLE public.events ADD COLUMN IF NOT EXISTS admin_message_id BIGINT;
ALTER TABLE public.events ADD COLUMN IF NOT EXISTS location_channel_id BIGINT;
ALTER TABLE public.events ADD COLUMN IF NOT EXISTS location_text TEXT;
ALTER TABLE public.events ADD COLUMN IF NOT EXISTS registration_type TEXT NOT NULL DEFAULT 'DIRECT';
ALTER TABLE public.events ADD COLUMN IF NOT EXISTS form_questions JSONB DEFAULT '[]'::jsonb;
ALTER TABLE public.events ADD COLUMN IF NOT EXISTS prizes TEXT;
ALTER TABLE public.events ADD COLUMN IF NOT EXISTS banner_url TEXT;
ALTER TABLE public.events ADD COLUMN IF NOT EXISTS role_id BIGINT;
ALTER TABLE public.events ADD COLUMN IF NOT EXISTS enable_waitlist BOOLEAN NOT NULL DEFAULT TRUE;

-- 3. Event Participants enhancements
ALTER TABLE public.event_participants ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'REGISTERED';
ALTER TABLE public.event_participants ADD COLUMN IF NOT EXISTS form_answers JSONB DEFAULT '{}'::jsonb;
ALTER TABLE public.event_participants ADD COLUMN IF NOT EXISTS waitlist_position INTEGER;
ALTER TABLE public.event_participants ADD COLUMN IF NOT EXISTS reviewed_by BIGINT;
ALTER TABLE public.event_participants ADD COLUMN IF NOT EXISTS review_note TEXT;

CREATE INDEX IF NOT EXISTS idx_event_participants_status ON public.event_participants(event_id, status);

-- 4. No-Show Strikes & Suspensions
CREATE TABLE IF NOT EXISTS public.event_strikes (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    guild_id BIGINT NOT NULL,
    user_id BIGINT NOT NULL,
    event_id BIGINT NOT NULL REFERENCES public.events(id) ON DELETE CASCADE,
    recorded_by BIGINT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_event_strikes_user ON public.event_strikes(guild_id, user_id);

CREATE TABLE IF NOT EXISTS public.event_suspensions (
    guild_id BIGINT NOT NULL,
    user_id BIGINT NOT NULL,
    suspended_until TIMESTAMPTZ NOT NULL,
    reason TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (guild_id, user_id)
);
CREATE INDEX IF NOT EXISTS idx_event_suspensions_until ON public.event_suspensions(guild_id, suspended_until);

-- 5. Advanced Registration Function
CREATE OR REPLACE FUNCTION public.register_event_participant_advanced(
    p_event_id BIGINT,
    p_guild_id BIGINT,
    p_user_id BIGINT,
    p_user_name TEXT,
    p_form_answers JSONB DEFAULT '{}'::jsonb
) RETURNS JSONB LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
DECLARE
    v_event public.events%ROWTYPE;
    v_count INTEGER;
    v_waitlist_count INTEGER;
    v_suspension public.event_suspensions%ROWTYPE;
    v_status TEXT;
    v_pos INTEGER := NULL;
    v_participant_id BIGINT;
BEGIN
    -- Check suspension
    SELECT * INTO v_suspension FROM public.event_suspensions
    WHERE guild_id = p_guild_id AND user_id = p_user_id AND suspended_until > NOW();
    IF FOUND THEN
        RETURN jsonb_build_object(
            'result', 'suspended',
            'suspended_until', v_suspension.suspended_until,
            'reason', v_suspension.reason
        );
    END IF;

    -- Lock event
    SELECT * INTO v_event FROM public.events
    WHERE id = p_event_id AND guild_id = p_guild_id FOR UPDATE;

    IF NOT FOUND THEN
        RETURN jsonb_build_object('result', 'not_found');
    END IF;

    IF v_event.status <> 'OPEN' OR v_event.starts_at <= NOW() THEN
        RETURN jsonb_build_object('result', 'closed');
    END IF;

    -- Check duplicate
    IF EXISTS (
        SELECT 1 FROM public.event_participants
        WHERE event_id = p_event_id AND user_id = p_user_id AND status IN ('REGISTERED', 'WAITLIST', 'PENDING')
    ) THEN
        RETURN jsonb_build_object('result', 'duplicate');
    END IF;

    -- Registration Type: FORM
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

    -- Registration Type: DIRECT
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
REVOKE ALL ON FUNCTION public.register_event_participant_advanced(BIGINT, BIGINT, BIGINT, TEXT, JSONB) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.register_event_participant_advanced(BIGINT, BIGINT, BIGINT, TEXT, JSONB) TO service_role;

-- 6. Cancel Registration & Auto-Promote Waitlist
CREATE OR REPLACE FUNCTION public.cancel_event_registration(
    p_event_id BIGINT,
    p_guild_id BIGINT,
    p_user_id BIGINT
) RETURNS JSONB LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
DECLARE
    v_target public.event_participants%ROWTYPE;
    v_promoted public.event_participants%ROWTYPE;
BEGIN
    SELECT * INTO v_target FROM public.event_participants
    WHERE event_id = p_event_id AND guild_id = p_guild_id AND user_id = p_user_id
    FOR UPDATE;

    IF NOT FOUND OR v_target.status NOT IN ('REGISTERED', 'WAITLIST', 'PENDING') THEN
        RETURN jsonb_build_object('result', 'not_registered');
    END IF;

    -- Delete or mark cancelled
    DELETE FROM public.event_participants WHERE id = v_target.id;

    -- If cancelled user was REGISTERED, promote top waitlist user
    IF v_target.status = 'REGISTERED' THEN
        SELECT * INTO v_promoted FROM public.event_participants
        WHERE event_id = p_event_id AND status = 'WAITLIST'
        ORDER BY waitlist_position ASC, created_at ASC
        LIMIT 1 FOR UPDATE;

        IF FOUND THEN
            UPDATE public.event_participants
            SET status = 'REGISTERED', waitlist_position = NULL
            WHERE id = v_promoted.id;

            -- Re-order remaining waitlist
            WITH reordered AS (
                SELECT id, row_number() OVER (ORDER BY waitlist_position ASC, created_at ASC) as new_pos
                FROM public.event_participants
                WHERE event_id = p_event_id AND status = 'WAITLIST'
            )
            UPDATE public.event_participants ep
            SET waitlist_position = reordered.new_pos
            FROM reordered
            WHERE ep.id = reordered.id;

            RETURN jsonb_build_object(
                'result', 'cancelled_and_promoted',
                'promoted_user_id', v_promoted.user_id,
                'promoted_user_name', v_promoted.user_name
            );
        END IF;
    END IF;

    RETURN jsonb_build_object('result', 'cancelled');
END;
$$;
REVOKE ALL ON FUNCTION public.cancel_event_registration(BIGINT, BIGINT, BIGINT) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.cancel_event_registration(BIGINT, BIGINT, BIGINT) TO service_role;

-- 7. Record No-Show & Trigger Suspension after 3 strikes
CREATE OR REPLACE FUNCTION public.record_event_no_show(
    p_guild_id BIGINT,
    p_event_id BIGINT,
    p_user_id BIGINT,
    p_recorded_by BIGINT
) RETURNS JSONB LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
DECLARE
    v_strikes_count INTEGER;
    v_suspended BOOLEAN := FALSE;
    v_until TIMESTAMPTZ;
BEGIN
    -- Record strike
    INSERT INTO public.event_strikes(guild_id, user_id, event_id, recorded_by)
    VALUES (p_guild_id, p_user_id, p_event_id, p_recorded_by);

    -- Count active strikes in past 90 days
    SELECT count(*) INTO v_strikes_count
    FROM public.event_strikes
    WHERE guild_id = p_guild_id AND user_id = p_user_id
      AND created_at >= NOW() - INTERVAL '90 days';

    IF v_strikes_count >= 3 THEN
        v_until := NOW() + INTERVAL '7 days';
        INSERT INTO public.event_suspensions(guild_id, user_id, suspended_until, reason)
        VALUES (p_guild_id, p_user_id, v_until, 'تكرار عدم حضور الفعاليات (3 مرات)')
        ON CONFLICT (guild_id, user_id)
        DO UPDATE SET suspended_until = v_until, reason = 'تكرار عدم حضور الفعاليات (3 مرات)';

        v_suspended := TRUE;
    END IF;

    RETURN jsonb_build_object(
        'strikes', v_strikes_count,
        'suspended', v_suspended,
        'suspended_until', v_until
    );
END;
$$;
REVOKE ALL ON FUNCTION public.record_event_no_show(BIGINT, BIGINT, BIGINT, BIGINT) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.record_event_no_show(BIGINT, BIGINT, BIGINT, BIGINT) TO service_role;
