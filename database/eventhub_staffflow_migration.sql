-- EventHub + StaffFlow. Run once in the Supabase SQL editor.

CREATE TABLE IF NOT EXISTS public.events (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    guild_id BIGINT NOT NULL,
    created_by BIGINT NOT NULL,
    title TEXT NOT NULL CHECK (char_length(title) BETWEEN 1 AND 100),
    description TEXT NOT NULL CHECK (char_length(description) BETWEEN 1 AND 1800),
    channel_id BIGINT NOT NULL,
    announcement_message_id BIGINT,
    starts_at TIMESTAMPTZ NOT NULL,
    capacity INTEGER NOT NULL CHECK (capacity BETWEEN 1 AND 500),
    status TEXT NOT NULL DEFAULT 'OPEN' CHECK (status IN ('OPEN', 'CLOSED', 'CANCELLED')),
    reminder_at TIMESTAMPTZ,
    reminded_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    closed_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_events_reminders ON public.events(reminder_at) WHERE status = 'OPEN' AND reminded_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_events_guild_start ON public.events(guild_id, starts_at) WHERE status = 'OPEN';

CREATE TABLE IF NOT EXISTS public.event_participants (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    event_id BIGINT NOT NULL REFERENCES public.events(id) ON DELETE CASCADE,
    guild_id BIGINT NOT NULL,
    user_id BIGINT NOT NULL,
    user_name TEXT NOT NULL,
    attended BOOLEAN,
    attendance_by BIGINT,
    attendance_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(event_id, user_id)
);
CREATE INDEX IF NOT EXISTS idx_event_participants_event ON public.event_participants(event_id);

-- Serialize registration on the event row so simultaneous signups cannot exceed capacity.
CREATE OR REPLACE FUNCTION public.register_event_participant(
    p_event_id BIGINT, p_guild_id BIGINT, p_user_id BIGINT, p_user_name TEXT
) RETURNS JSONB LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
DECLARE v_event public.events%ROWTYPE; v_count INTEGER;
BEGIN
    SELECT * INTO v_event FROM public.events WHERE id = p_event_id AND guild_id = p_guild_id FOR UPDATE;
    IF NOT FOUND THEN RETURN jsonb_build_object('result', 'not_found'); END IF;
    IF v_event.status <> 'OPEN' OR v_event.starts_at <= NOW() THEN
        RETURN jsonb_build_object('result', 'closed');
    END IF;
    IF EXISTS (SELECT 1 FROM public.event_participants WHERE event_id = p_event_id AND user_id = p_user_id) THEN
        RETURN jsonb_build_object('result', 'duplicate');
    END IF;
    SELECT count(*) INTO v_count FROM public.event_participants WHERE event_id = p_event_id;
    IF v_count >= v_event.capacity THEN RETURN jsonb_build_object('result', 'full'); END IF;
    INSERT INTO public.event_participants(event_id, guild_id, user_id, user_name)
    VALUES (p_event_id, p_guild_id, p_user_id, p_user_name);
    RETURN jsonb_build_object('result', 'registered');
END;
$$;
REVOKE ALL ON FUNCTION public.register_event_participant(BIGINT, BIGINT, BIGINT, TEXT) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.register_event_participant(BIGINT, BIGINT, BIGINT, TEXT) TO service_role;

CREATE TABLE IF NOT EXISTS public.staff_cases (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    guild_id BIGINT NOT NULL,
    user_id BIGINT NOT NULL,
    user_name TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('إجازة', 'شكوى')),
    subject TEXT NOT NULL CHECK (char_length(subject) BETWEEN 1 AND 120),
    details TEXT NOT NULL CHECK (char_length(details) BETWEEN 1 AND 1800),
    starts_at DATE,
    ends_at DATE,
    status TEXT NOT NULL DEFAULT 'PENDING' CHECK (status IN ('PENDING', 'APPROVED', 'REJECTED', 'DELIVERY_FAILED')),
    review_channel_id BIGINT,
    review_message_id BIGINT,
    reviewed_by BIGINT,
    decision_note TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    reviewed_at TIMESTAMPTZ,
    CHECK ((starts_at IS NULL AND ends_at IS NULL) OR (starts_at IS NOT NULL AND ends_at >= starts_at))
);
CREATE INDEX IF NOT EXISTS idx_staff_cases_pending ON public.staff_cases(guild_id, status, created_at DESC);

CREATE TABLE IF NOT EXISTS public.staff_shifts (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    guild_id BIGINT NOT NULL,
    user_id BIGINT NOT NULL,
    created_by BIGINT NOT NULL,
    starts_at TIMESTAMPTZ NOT NULL,
    ends_at TIMESTAMPTZ NOT NULL CHECK (ends_at > starts_at),
    duties TEXT NOT NULL CHECK (char_length(duties) BETWEEN 1 AND 1000),
    status TEXT NOT NULL DEFAULT 'SCHEDULED' CHECK (status IN ('SCHEDULED', 'CANCELLED', 'COMPLETED')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_staff_shifts_upcoming ON public.staff_shifts(guild_id, user_id, starts_at) WHERE status = 'SCHEDULED';

CREATE TABLE IF NOT EXISTS public.staff_audit (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    guild_id BIGINT NOT NULL,
    actor_id BIGINT NOT NULL,
    target_user_id BIGINT NOT NULL,
    action TEXT NOT NULL,
    details JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_staff_audit_target ON public.staff_audit(guild_id, target_user_id, created_at DESC);

CREATE OR REPLACE FUNCTION public.review_staff_case(
    p_case_id BIGINT, p_guild_id BIGINT, p_reviewer_id BIGINT, p_decision TEXT, p_note TEXT
) RETURNS JSONB LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
DECLARE v_case public.staff_cases%ROWTYPE;
BEGIN
    IF p_decision NOT IN ('APPROVED', 'REJECTED') THEN RAISE EXCEPTION 'Invalid staff case decision'; END IF;
    UPDATE public.staff_cases SET status = p_decision, reviewed_by = p_reviewer_id,
        decision_note = left(p_note, 1000), reviewed_at = NOW()
    WHERE id = p_case_id AND guild_id = p_guild_id AND status = 'PENDING'
    RETURNING * INTO v_case;
    IF NOT FOUND THEN RETURN jsonb_build_object('changed', FALSE); END IF;
    INSERT INTO public.staff_audit(guild_id, actor_id, target_user_id, action, details)
    VALUES (p_guild_id, p_reviewer_id, v_case.user_id, 'CASE_' || p_decision,
        jsonb_build_object('case_id', p_case_id, 'kind', v_case.kind, 'note', left(p_note, 1000)));
    RETURN jsonb_build_object('changed', TRUE, 'case', to_jsonb(v_case));
END;
$$;
REVOKE ALL ON FUNCTION public.review_staff_case(BIGINT, BIGINT, BIGINT, TEXT, TEXT) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.review_staff_case(BIGINT, BIGINT, BIGINT, TEXT, TEXT) TO service_role;

ALTER TABLE public.events ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.event_participants ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.staff_cases ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.staff_shifts ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.staff_audit ENABLE ROW LEVEL SECURITY;
