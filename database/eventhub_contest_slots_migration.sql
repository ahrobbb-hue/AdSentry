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

-- Preserve the first five existing open events per server and leave any
-- overflow open events untouched; they still count against the five-event cap.
WITH ranked AS (
    SELECT id, row_number() OVER (PARTITION BY guild_id ORDER BY created_at, id) AS slot
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

    IF (SELECT count(*) FROM public.events WHERE guild_id = p_guild_id AND status = 'OPEN') >= 5 THEN
        RETURN jsonb_build_object('created', FALSE, 'reason', 'limit');
    END IF;

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
