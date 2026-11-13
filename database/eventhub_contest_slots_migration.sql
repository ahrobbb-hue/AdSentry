-- Limit each server to five concurrent EventHub events and reserve one
-- distinct participant role slot for every open event.
-- Run after eventhub_staffflow_migration.sql and eventhub_advanced_migration.sql.

ALTER TABLE public.events
    ADD COLUMN IF NOT EXISTS role_slot SMALLINT;
ALTER TABLE public.events
    ADD COLUMN IF NOT EXISTS role_id BIGINT;

ALTER TABLE public.events
    DROP CONSTRAINT IF EXISTS events_role_slot_check;
ALTER TABLE public.events
    ADD CONSTRAINT events_role_slot_check
    CHECK (role_slot IS NULL OR role_slot BETWEEN 1 AND 5);

-- Keep the five soonest open events per server and close any existing overflow.
WITH ranked AS (
    SELECT id, row_number() OVER (PARTITION BY guild_id ORDER BY starts_at, created_at, id) AS position
    FROM public.events
    WHERE status = 'OPEN'
)
UPDATE public.events AS event
SET status = 'CLOSED',
    closed_at = COALESCE(event.closed_at, NOW())
FROM ranked
WHERE event.id = ranked.id
  AND ranked.position > 5;

WITH ranked AS (
    SELECT id, row_number() OVER (PARTITION BY guild_id ORDER BY starts_at, created_at, id) AS slot
    FROM public.events
    WHERE status = 'OPEN'
)
UPDATE public.events AS event
SET role_slot = ranked.slot::SMALLINT
FROM ranked
WHERE event.id = ranked.id
  AND ranked.slot <= 5
  AND event.role_slot IS NULL;

CREATE UNIQUE INDEX IF NOT EXISTS idx_events_open_role_slot
    ON public.events(guild_id, role_slot)
    WHERE status = 'OPEN' AND role_slot IS NOT NULL;

CREATE OR REPLACE FUNCTION public.create_event_with_slot(
    p_guild_id BIGINT,
    p_created_by BIGINT,
    p_title TEXT,
    p_description TEXT,
    p_channel_id BIGINT,
    p_starts_at TIMESTAMPTZ,
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
        guild_id, created_by, title, description, channel_id, starts_at,
        capacity, status, reminder_at, registration_type, form_questions,
        prizes, admin_channel_id, enable_waitlist, role_slot, role_id
    ) VALUES (
        p_guild_id, p_created_by, p_title, p_description, p_channel_id, p_starts_at,
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
    BIGINT, BIGINT, TEXT, TEXT, BIGINT, TIMESTAMPTZ, INTEGER, TIMESTAMPTZ,
    TEXT, JSONB, TEXT, BIGINT, BIGINT[]
) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.create_event_with_slot(
    BIGINT, BIGINT, TEXT, TEXT, BIGINT, TIMESTAMPTZ, INTEGER, TIMESTAMPTZ,
    TEXT, JSONB, TEXT, BIGINT, BIGINT[]
) TO service_role;

CREATE OR REPLACE FUNCTION public.update_event_details(
    p_event_id BIGINT,
    p_guild_id BIGINT,
    p_title TEXT,
    p_description TEXT,
    p_capacity INTEGER,
    p_starts_at TIMESTAMPTZ
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
       OR p_starts_at IS NULL OR p_starts_at <= NOW() THEN
        RAISE EXCEPTION 'Invalid event title, description, capacity or start time';
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
        reminder_at = p_starts_at - INTERVAL '24 hours',
        reminded_at = NULL
    WHERE id = p_event_id
    RETURNING * INTO v_event;

    RETURN jsonb_build_object('updated', TRUE, 'event', to_jsonb(v_event));
END;
$$;
REVOKE ALL ON FUNCTION public.update_event_details(BIGINT, BIGINT, TEXT, TEXT, INTEGER, TIMESTAMPTZ)
    FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.update_event_details(BIGINT, BIGINT, TEXT, TEXT, INTEGER, TIMESTAMPTZ)
    TO service_role;

CREATE OR REPLACE FUNCTION public.cancel_event(
    p_event_id BIGINT,
    p_guild_id BIGINT
) RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
    v_event public.events%ROWTYPE;
BEGIN
    SELECT * INTO v_event
    FROM public.events
    WHERE id = p_event_id AND guild_id = p_guild_id
    FOR UPDATE;

    IF NOT FOUND THEN
        RETURN jsonb_build_object('cancelled', FALSE, 'reason', 'not_found');
    END IF;
    IF v_event.status <> 'OPEN' THEN
        RETURN jsonb_build_object('cancelled', FALSE, 'reason', 'closed');
    END IF;

    UPDATE public.events
    SET status = 'CANCELLED', closed_at = NOW()
    WHERE id = p_event_id
    RETURNING * INTO v_event;

    RETURN jsonb_build_object('cancelled', TRUE, 'event', to_jsonb(v_event));
END;
$$;
REVOKE ALL ON FUNCTION public.cancel_event(BIGINT, BIGINT) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.cancel_event(BIGINT, BIGINT) TO service_role;

CREATE OR REPLACE FUNCTION public.enforce_event_open_limit()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
    v_open_count INTEGER;
BEGIN
    IF NEW.status <> 'OPEN' THEN
        RETURN NEW;
    END IF;

    PERFORM pg_advisory_xact_lock(NEW.guild_id);
    SELECT count(*) INTO v_open_count
    FROM public.events
    WHERE guild_id = NEW.guild_id
      AND status = 'OPEN'
      AND id IS DISTINCT FROM NEW.id;

    IF v_open_count >= 5 THEN
        RAISE EXCEPTION 'A server can have at most five open events'
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$;
REVOKE ALL ON FUNCTION public.enforce_event_open_limit() FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.enforce_event_open_limit() TO service_role;

DROP TRIGGER IF EXISTS events_enforce_open_limit_insert ON public.events;
CREATE TRIGGER events_enforce_open_limit_insert
    BEFORE INSERT ON public.events
    FOR EACH ROW EXECUTE FUNCTION public.enforce_event_open_limit();

DROP TRIGGER IF EXISTS events_enforce_open_limit_update ON public.events;
CREATE TRIGGER events_enforce_open_limit_update
    BEFORE UPDATE OF guild_id, status ON public.events
    FOR EACH ROW EXECUTE FUNCTION public.enforce_event_open_limit();
