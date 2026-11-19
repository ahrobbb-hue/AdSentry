-- Auditable, manually curated EventHub raffles with a public drand beacon.

CREATE TABLE IF NOT EXISTS public.event_raffles (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    guild_id BIGINT NOT NULL,
    event_id BIGINT REFERENCES public.events(id) ON DELETE SET NULL,
    title TEXT NOT NULL CHECK (char_length(title) BETWEEN 1 AND 100),
    channel_id BIGINT NOT NULL,
    created_by BIGINT NOT NULL,
    winner_count INTEGER NOT NULL DEFAULT 1 CHECK (winner_count BETWEEN 1 AND 20),
    status TEXT NOT NULL DEFAULT 'OPEN'
        CHECK (status IN ('OPEN', 'LOCKED', 'DRAWN', 'CANCELLED')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    locked_at TIMESTAMPTZ,
    entrants_snapshot JSONB,
    entrants_hash TEXT,
    beacon_chain_hash TEXT,
    beacon_target_round BIGINT,
    lock_message_id BIGINT,
    spin_message_id BIGINT,
    winner_ids JSONB,
    beacon_randomness TEXT,
    beacon_signature TEXT,
    drawn_at TIMESTAMPTZ,
    result_message_id BIGINT
);
ALTER TABLE public.event_raffles ADD COLUMN IF NOT EXISTS event_id BIGINT REFERENCES public.events(id) ON DELETE SET NULL;
ALTER TABLE public.event_raffles ADD COLUMN IF NOT EXISTS spin_message_id BIGINT;
CREATE UNIQUE INDEX IF NOT EXISTS idx_event_raffles_event
    ON public.event_raffles(event_id)
    WHERE event_id IS NOT NULL AND status <> 'CANCELLED';

CREATE INDEX IF NOT EXISTS idx_event_raffles_guild_status
    ON public.event_raffles(guild_id, status, created_at DESC);

CREATE TABLE IF NOT EXISTS public.event_raffle_entries (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    raffle_id BIGINT NOT NULL REFERENCES public.event_raffles(id) ON DELETE CASCADE,
    guild_id BIGINT NOT NULL,
    user_id BIGINT NOT NULL,
    user_name TEXT NOT NULL,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    added_by BIGINT NOT NULL,
    added_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    removed_by BIGINT,
    removed_at TIMESTAMPTZ,
    UNIQUE(raffle_id, user_id)
);

CREATE INDEX IF NOT EXISTS idx_event_raffle_entries_active
    ON public.event_raffle_entries(raffle_id, user_id)
    WHERE is_active = TRUE;

CREATE TABLE IF NOT EXISTS public.event_raffle_audit (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    raffle_id BIGINT NOT NULL REFERENCES public.event_raffles(id) ON DELETE CASCADE,
    guild_id BIGINT NOT NULL,
    actor_id BIGINT NOT NULL,
    action TEXT NOT NULL,
    target_user_id BIGINT,
    details JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS public.event_raffle_claims (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    raffle_id BIGINT NOT NULL REFERENCES public.event_raffles(id) ON DELETE CASCADE,
    guild_id BIGINT NOT NULL,
    user_id BIGINT NOT NULL,
    token_hash TEXT NOT NULL CHECK (token_hash ~ '^[0-9a-fA-F]{64}$'),
    issued_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    redeemed_at TIMESTAMPTZ,
    redeemed_by BIGINT,
    UNIQUE(raffle_id, user_id)
);

CREATE INDEX IF NOT EXISTS idx_event_raffle_audit
    ON public.event_raffle_audit(raffle_id, created_at);
CREATE INDEX IF NOT EXISTS idx_event_raffle_claims_pending
    ON public.event_raffle_claims(raffle_id, user_id)
    WHERE redeemed_at IS NULL;

CREATE OR REPLACE FUNCTION public.guard_event_raffle_entry()
RETURNS TRIGGER
LANGUAGE plpgsql
SET search_path = public
AS $$
DECLARE
    v_status TEXT;
BEGIN
    SELECT status INTO v_status
    FROM public.event_raffles
    WHERE id = NEW.raffle_id
    FOR UPDATE;

    IF v_status IS DISTINCT FROM 'OPEN' THEN
        RAISE EXCEPTION 'Raffle entries can only change while the raffle is OPEN';
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS event_raffle_entry_open_only ON public.event_raffle_entries;
CREATE TRIGGER event_raffle_entry_open_only
BEFORE INSERT OR UPDATE ON public.event_raffle_entries
FOR EACH ROW EXECUTE FUNCTION public.guard_event_raffle_entry();

DROP FUNCTION IF EXISTS public.create_event_raffle(BIGINT, BIGINT, TEXT, BIGINT, INTEGER);
CREATE OR REPLACE FUNCTION public.create_event_raffle(
    p_guild_id BIGINT,
    p_created_by BIGINT,
    p_title TEXT,
    p_channel_id BIGINT,
    p_winner_count INTEGER,
    p_event_id BIGINT DEFAULT NULL
) RETURNS JSONB
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public
AS $$
DECLARE
    v_raffle public.event_raffles%ROWTYPE;
    v_event public.events%ROWTYPE;
BEGIN
    IF char_length(trim(p_title)) NOT BETWEEN 1 AND 100
       OR p_winner_count NOT BETWEEN 1 AND 20 THEN
        RAISE EXCEPTION 'Invalid raffle title or winner count';
    END IF;

    IF p_event_id IS NOT NULL THEN
        SELECT * INTO v_event FROM public.events
        WHERE id = p_event_id
          AND guild_id = p_guild_id
          AND (
              status = 'CLOSED'
              OR (status = 'OPEN' AND registration_open = FALSE)
          );
        IF NOT FOUND THEN
            RAISE EXCEPTION 'Raffles require an ended event or closed registration in this server';
        END IF;
        IF EXISTS (
            SELECT 1 FROM public.event_raffles
            WHERE event_id = p_event_id AND status <> 'CANCELLED'
        ) THEN
            RAISE EXCEPTION 'This event already has a raffle';
        END IF;
    END IF;

    INSERT INTO public.event_raffles(guild_id, event_id, title, channel_id, created_by, winner_count)
    VALUES (p_guild_id, p_event_id, trim(p_title), p_channel_id, p_created_by, p_winner_count)
    RETURNING * INTO v_raffle;

    INSERT INTO public.event_raffle_audit(raffle_id, guild_id, actor_id, action, details)
    VALUES (v_raffle.id, p_guild_id, p_created_by, 'CREATED',
        jsonb_build_object('title', v_raffle.title, 'channel_id', p_channel_id,
                           'winner_count', p_winner_count));
    RETURN to_jsonb(v_raffle);
END;
$$;

CREATE OR REPLACE FUNCTION public.manage_event_raffle_entry(
    p_raffle_id BIGINT,
    p_guild_id BIGINT,
    p_actor_id BIGINT,
    p_user_id BIGINT,
    p_user_name TEXT,
    p_action TEXT,
    p_reason TEXT DEFAULT NULL
) RETURNS JSONB
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public
AS $$
DECLARE
    v_raffle public.event_raffles%ROWTYPE;
    v_entry public.event_raffle_entries%ROWTYPE;
BEGIN
    SELECT * INTO v_raffle
    FROM public.event_raffles
    WHERE id = p_raffle_id AND guild_id = p_guild_id
    FOR UPDATE;
    IF NOT FOUND THEN RAISE EXCEPTION 'Raffle not found in this server'; END IF;
    IF v_raffle.status <> 'OPEN' THEN RAISE EXCEPTION 'Raffle is not open for entry changes'; END IF;

    IF p_action = 'ADD' THEN
        INSERT INTO public.event_raffle_entries AS existing_entry(
            raffle_id, guild_id, user_id, user_name, added_by
        ) VALUES (
            p_raffle_id, p_guild_id, p_user_id, left(p_user_name, 100), p_actor_id
        )
        ON CONFLICT (raffle_id, user_id) DO UPDATE
        SET user_name = EXCLUDED.user_name, is_active = TRUE,
            added_by = p_actor_id, added_at = NOW(), removed_by = NULL, removed_at = NULL
        WHERE existing_entry.is_active = FALSE
        RETURNING * INTO v_entry;

        IF NOT FOUND THEN RAISE EXCEPTION 'Member is already in this raffle'; END IF;
        INSERT INTO public.event_raffle_audit(
            raffle_id, guild_id, actor_id, action, target_user_id, details
        ) VALUES (
            p_raffle_id, p_guild_id, p_actor_id, 'MEMBER_ADDED', p_user_id,
            jsonb_build_object('user_name', left(p_user_name, 100))
        );
    ELSIF p_action = 'REMOVE' THEN
        UPDATE public.event_raffle_entries
        SET is_active = FALSE, removed_by = p_actor_id, removed_at = NOW()
        WHERE raffle_id = p_raffle_id AND user_id = p_user_id AND is_active = TRUE
        RETURNING * INTO v_entry;

        IF NOT FOUND THEN RAISE EXCEPTION 'Member is not an active raffle entrant'; END IF;
        INSERT INTO public.event_raffle_audit(
            raffle_id, guild_id, actor_id, action, target_user_id, details
        ) VALUES (
            p_raffle_id, p_guild_id, p_actor_id, 'MEMBER_REMOVED', p_user_id,
            jsonb_build_object('reason', left(COALESCE(p_reason, ''), 300))
        );
    ELSE
        RAISE EXCEPTION 'Invalid raffle entry action';
    END IF;

    RETURN jsonb_build_object('changed', TRUE, 'entry', to_jsonb(v_entry));
END;
$$;

CREATE OR REPLACE FUNCTION public.lock_event_raffle(
    p_raffle_id BIGINT,
    p_guild_id BIGINT,
    p_actor_id BIGINT
) RETURNS JSONB
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public
AS $$
DECLARE
    v_raffle public.event_raffles%ROWTYPE;
    v_entries JSONB;
BEGIN
    SELECT * INTO v_raffle
    FROM public.event_raffles
    WHERE id = p_raffle_id AND guild_id = p_guild_id
    FOR UPDATE;
    IF NOT FOUND THEN RAISE EXCEPTION 'Raffle not found in this server'; END IF;

    IF v_raffle.status = 'OPEN' THEN
        SELECT COALESCE(
            jsonb_agg(jsonb_build_object('user_id', user_id, 'user_name', user_name) ORDER BY user_id),
            '[]'::jsonb
        ) INTO v_entries
        FROM public.event_raffle_entries
        WHERE raffle_id = p_raffle_id AND is_active = TRUE;

        IF jsonb_array_length(v_entries) = 0 THEN
            RAISE EXCEPTION 'Cannot lock a raffle with no entrants';
        END IF;
        IF v_raffle.winner_count > jsonb_array_length(v_entries) THEN
            RAISE EXCEPTION 'Winner count cannot exceed the number of entrants';
        END IF;

        UPDATE public.event_raffles
        SET status = 'LOCKED', locked_at = NOW(), entrants_snapshot = v_entries
        WHERE id = p_raffle_id
        RETURNING * INTO v_raffle;

        INSERT INTO public.event_raffle_audit(raffle_id, guild_id, actor_id, action, details)
        VALUES (p_raffle_id, p_guild_id, p_actor_id, 'ENTRANTS_LOCKED',
            jsonb_build_object('entrant_count', jsonb_array_length(v_entries)));
    ELSIF v_raffle.status NOT IN ('LOCKED', 'DRAWN') THEN
        RAISE EXCEPTION 'Raffle cannot be locked in its current state';
    END IF;

    RETURN to_jsonb(v_raffle);
END;
$$;

CREATE OR REPLACE FUNCTION public.prepare_event_raffle_beacon(
    p_raffle_id BIGINT,
    p_guild_id BIGINT,
    p_actor_id BIGINT,
    p_entrants_hash TEXT,
    p_chain_hash TEXT,
    p_target_round BIGINT
) RETURNS JSONB
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public
AS $$
DECLARE
    v_raffle public.event_raffles%ROWTYPE;
BEGIN
    SELECT * INTO v_raffle
    FROM public.event_raffles
    WHERE id = p_raffle_id AND guild_id = p_guild_id
    FOR UPDATE;
    IF NOT FOUND OR v_raffle.status NOT IN ('LOCKED', 'DRAWN') THEN
        RAISE EXCEPTION 'Raffle must be locked before preparing a beacon';
    END IF;

    IF v_raffle.entrants_hash IS NOT NULL AND (
        v_raffle.entrants_hash <> p_entrants_hash
        OR v_raffle.beacon_chain_hash <> p_chain_hash
        OR v_raffle.beacon_target_round <> p_target_round
    ) THEN
        RAISE EXCEPTION 'The locked raffle proof cannot be changed';
    END IF;

    IF v_raffle.entrants_hash IS NULL THEN
        UPDATE public.event_raffles
        SET entrants_hash = p_entrants_hash,
            beacon_chain_hash = p_chain_hash,
            beacon_target_round = p_target_round
        WHERE id = p_raffle_id
        RETURNING * INTO v_raffle;

        INSERT INTO public.event_raffle_audit(raffle_id, guild_id, actor_id, action, details)
        VALUES (p_raffle_id, p_guild_id, p_actor_id, 'BEACON_COMMITTED',
            jsonb_build_object('entrants_hash', p_entrants_hash,
                               'chain_hash', p_chain_hash, 'target_round', p_target_round));
    END IF;
    RETURN to_jsonb(v_raffle);
END;
$$;

DROP FUNCTION IF EXISTS public.record_event_raffle_result(BIGINT, BIGINT, BIGINT, JSONB, TEXT, TEXT, BIGINT, TEXT, TEXT);
CREATE OR REPLACE FUNCTION public.record_event_raffle_result(
    p_raffle_id BIGINT,
    p_guild_id BIGINT,
    p_actor_id BIGINT,
    p_winner_ids JSONB,
    p_entrants_hash TEXT,
    p_chain_hash TEXT,
    p_round BIGINT,
    p_randomness TEXT,
    p_signature TEXT,
    p_claim_hashes JSONB
) RETURNS JSONB
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public
AS $$
DECLARE
    v_raffle public.event_raffles%ROWTYPE;
BEGIN
    SELECT * INTO v_raffle
    FROM public.event_raffles
    WHERE id = p_raffle_id AND guild_id = p_guild_id
    FOR UPDATE;
    IF NOT FOUND THEN RAISE EXCEPTION 'Raffle not found in this server'; END IF;
    IF v_raffle.status = 'DRAWN' THEN
        RETURN jsonb_build_object('changed', FALSE, 'raffle', to_jsonb(v_raffle));
    END IF;
    IF v_raffle.status <> 'LOCKED'
       OR v_raffle.entrants_hash <> p_entrants_hash
       OR v_raffle.beacon_chain_hash <> p_chain_hash
       OR v_raffle.beacon_target_round <> p_round
       OR jsonb_array_length(p_winner_ids) <> v_raffle.winner_count
       OR p_randomness !~ '^[0-9A-Fa-f]{64}$'
       OR p_signature !~ '^[0-9A-Fa-f]{64,512}$' THEN
        RAISE EXCEPTION 'Raffle proof does not match the locked draw';
    END IF;

    IF (
        SELECT count(DISTINCT winner.user_id)
        FROM jsonb_array_elements_text(p_winner_ids) AS winner(user_id)
    ) <> v_raffle.winner_count THEN
        RAISE EXCEPTION 'Raffle winners must be unique';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM jsonb_array_elements_text(p_winner_ids) AS winner(user_id)
        WHERE NOT EXISTS (
            SELECT 1
            FROM jsonb_array_elements(v_raffle.entrants_snapshot) AS entrants(value)
            WHERE entrants.value->>'user_id' = winner.user_id
        )
    ) THEN
        RAISE EXCEPTION 'A selected winner is not in the locked entrant list';
    END IF;

    IF p_claim_hashes IS NULL OR jsonb_typeof(p_claim_hashes) <> 'array' THEN
        RAISE EXCEPTION 'Claim proofs must be an array';
    END IF;
    IF jsonb_array_length(p_claim_hashes) <> v_raffle.winner_count OR EXISTS (
        SELECT 1
        FROM jsonb_array_elements(p_claim_hashes) AS claim(value)
        WHERE claim.value->>'token_hash' !~ '^[0-9A-Fa-f]{64}$'
           OR NOT EXISTS (
                SELECT 1 FROM jsonb_array_elements_text(p_winner_ids) AS winner(user_id)
                WHERE winner.user_id = claim.value->>'user_id'
           )
    ) THEN
        RAISE EXCEPTION 'Claim proofs must exist for every selected winner';
    END IF;

    IF (
        SELECT count(DISTINCT claim.value->>'user_id')
        FROM jsonb_array_elements(p_claim_hashes) AS claim(value)
    ) <> v_raffle.winner_count THEN
        RAISE EXCEPTION 'Claim proofs must be unique per winner';
    END IF;

    UPDATE public.event_raffles
    SET status = 'DRAWN', winner_ids = p_winner_ids,
        beacon_randomness = p_randomness, beacon_signature = p_signature,
        drawn_at = NOW()
    WHERE id = p_raffle_id
    RETURNING * INTO v_raffle;

    INSERT INTO public.event_raffle_claims(raffle_id, guild_id, user_id, token_hash)
    SELECT p_raffle_id, p_guild_id, (claim.value->>'user_id')::BIGINT, lower(claim.value->>'token_hash')
    FROM jsonb_array_elements(p_claim_hashes) AS claim(value);

    INSERT INTO public.event_raffle_audit(raffle_id, guild_id, actor_id, action, details)
    VALUES (p_raffle_id, p_guild_id, p_actor_id, 'DRAW_COMPLETED',
        jsonb_build_object('winner_ids', p_winner_ids, 'round', p_round,
                           'chain_hash', p_chain_hash, 'randomness', p_randomness,
                           'entrants_hash', p_entrants_hash));
    RETURN jsonb_build_object('changed', TRUE, 'raffle', to_jsonb(v_raffle));
END;
$$;

CREATE OR REPLACE FUNCTION public.cancel_event_raffle(
    p_raffle_id BIGINT,
    p_guild_id BIGINT,
    p_actor_id BIGINT,
    p_reason TEXT
) RETURNS JSONB
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public
AS $$
DECLARE
    v_raffle public.event_raffles%ROWTYPE;
BEGIN
    UPDATE public.event_raffles
    SET status = 'CANCELLED'
    WHERE id = p_raffle_id AND guild_id = p_guild_id AND status = 'OPEN'
    RETURNING * INTO v_raffle;
    IF NOT FOUND THEN RAISE EXCEPTION 'Only an OPEN raffle can be cancelled'; END IF;

    INSERT INTO public.event_raffle_audit(raffle_id, guild_id, actor_id, action, details)
    VALUES (p_raffle_id, p_guild_id, p_actor_id, 'CANCELLED',
        jsonb_build_object('reason', left(COALESCE(p_reason, ''), 300)));
    RETURN to_jsonb(v_raffle);
END;
$$;

CREATE OR REPLACE FUNCTION public.set_event_raffle_lock_message(
    p_raffle_id BIGINT,
    p_guild_id BIGINT,
    p_message_id BIGINT
) RETURNS BOOLEAN
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public
AS $$
BEGIN
    UPDATE public.event_raffles
    SET lock_message_id = p_message_id
    WHERE id = p_raffle_id AND guild_id = p_guild_id
      AND status IN ('LOCKED', 'DRAWN') AND lock_message_id IS NULL;
    RETURN FOUND;
END;
$$;

CREATE OR REPLACE FUNCTION public.set_event_raffle_result_message(
    p_raffle_id BIGINT,
    p_guild_id BIGINT,
    p_message_id BIGINT
) RETURNS BOOLEAN
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public
AS $$
BEGIN
    UPDATE public.event_raffles
    SET result_message_id = p_message_id
    WHERE id = p_raffle_id AND guild_id = p_guild_id
      AND status = 'DRAWN' AND result_message_id IS NULL;
    RETURN FOUND;
END;
$$;

CREATE OR REPLACE FUNCTION public.set_event_raffle_spin_message(
    p_raffle_id BIGINT,
    p_guild_id BIGINT,
    p_message_id BIGINT
) RETURNS BOOLEAN
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public
AS $$
BEGIN
    UPDATE public.event_raffles
    SET spin_message_id = p_message_id
    WHERE id = p_raffle_id AND guild_id = p_guild_id
      AND status = 'LOCKED' AND spin_message_id IS NULL;
    RETURN FOUND;
END;
$$;

DROP FUNCTION IF EXISTS public.issue_event_raffle_claim(BIGINT, BIGINT, BIGINT, TEXT);
CREATE OR REPLACE FUNCTION public.issue_event_raffle_claim(
    p_raffle_id BIGINT,
    p_guild_id BIGINT,
    p_actor_id BIGINT,
    p_user_id BIGINT,
    p_token_hash TEXT
) RETURNS BOOLEAN
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public
AS $$
DECLARE
    v_status TEXT;
    v_winners JSONB;
BEGIN
    IF p_token_hash !~ '^[0-9A-Fa-f]{64}$' THEN
        RAISE EXCEPTION 'Invalid claim code hash';
    END IF;
    SELECT status, winner_ids INTO v_status, v_winners
    FROM public.event_raffles
    WHERE id = p_raffle_id AND guild_id = p_guild_id
    FOR UPDATE;
    IF v_status IS DISTINCT FROM 'DRAWN'
       OR NOT EXISTS (
            SELECT 1 FROM jsonb_array_elements_text(v_winners) AS winner(user_id)
            WHERE winner.user_id = p_user_id::TEXT
       ) THEN
        RAISE EXCEPTION 'Only a raffle winner can request a claim code';
    END IF;

    INSERT INTO public.event_raffle_claims(raffle_id, guild_id, user_id, token_hash)
    VALUES (p_raffle_id, p_guild_id, p_user_id, lower(p_token_hash))
    ON CONFLICT (raffle_id, user_id) DO UPDATE
    SET token_hash = EXCLUDED.token_hash, issued_at = NOW()
    WHERE public.event_raffle_claims.redeemed_at IS NULL;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'This prize has already been claimed';
    END IF;
    INSERT INTO public.event_raffle_audit(
        raffle_id, guild_id, actor_id, action, target_user_id, details
    ) VALUES (
        p_raffle_id, p_guild_id, p_actor_id, 'PRIZE_CLAIM_CODE_REISSUED', p_user_id,
        jsonb_build_object('issued_at', NOW())
    );
    RETURN TRUE;
END;
$$;

CREATE OR REPLACE FUNCTION public.verify_event_raffle_claim(
    p_raffle_id BIGINT,
    p_guild_id BIGINT,
    p_user_id BIGINT,
    p_actor_id BIGINT,
    p_token_hash TEXT
) RETURNS BOOLEAN
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public
AS $$
DECLARE
    v_claim public.event_raffle_claims%ROWTYPE;
BEGIN
    IF p_token_hash IS NULL OR p_token_hash !~ '^[0-9A-Fa-f]{64}$' THEN
        RETURN FALSE;
    END IF;
    SELECT * INTO v_claim FROM public.event_raffle_claims
    WHERE raffle_id = p_raffle_id AND guild_id = p_guild_id AND user_id = p_user_id
    FOR UPDATE;
    IF NOT FOUND OR v_claim.redeemed_at IS NOT NULL
       OR v_claim.token_hash <> lower(p_token_hash) THEN
        RETURN FALSE;
    END IF;

    UPDATE public.event_raffle_claims
    SET redeemed_at = NOW(), redeemed_by = p_actor_id
    WHERE id = v_claim.id;
    INSERT INTO public.event_raffle_audit(raffle_id, guild_id, actor_id, action, target_user_id, details)
    VALUES (p_raffle_id, p_guild_id, p_actor_id, 'PRIZE_CLAIM_VERIFIED', p_user_id,
        jsonb_build_object('redeemed', TRUE));
    RETURN TRUE;
END;
$$;

ALTER TABLE public.event_raffles ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.event_raffle_entries ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.event_raffle_audit ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.event_raffle_claims ENABLE ROW LEVEL SECURITY;
GRANT ALL ON public.event_raffles, public.event_raffle_entries, public.event_raffle_audit TO service_role;
GRANT ALL ON public.event_raffle_claims TO service_role;
GRANT USAGE, SELECT ON SEQUENCE public.event_raffles_id_seq,
    public.event_raffle_entries_id_seq, public.event_raffle_audit_id_seq,
    public.event_raffle_claims_id_seq TO service_role;

REVOKE ALL ON FUNCTION public.create_event_raffle(BIGINT, BIGINT, TEXT, BIGINT, INTEGER, BIGINT) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.manage_event_raffle_entry(BIGINT, BIGINT, BIGINT, BIGINT, TEXT, TEXT, TEXT) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.lock_event_raffle(BIGINT, BIGINT, BIGINT) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.prepare_event_raffle_beacon(BIGINT, BIGINT, BIGINT, TEXT, TEXT, BIGINT) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.record_event_raffle_result(BIGINT, BIGINT, BIGINT, JSONB, TEXT, TEXT, BIGINT, TEXT, TEXT, JSONB) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.cancel_event_raffle(BIGINT, BIGINT, BIGINT, TEXT) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.set_event_raffle_lock_message(BIGINT, BIGINT, BIGINT) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.set_event_raffle_result_message(BIGINT, BIGINT, BIGINT) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.set_event_raffle_spin_message(BIGINT, BIGINT, BIGINT) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.issue_event_raffle_claim(BIGINT, BIGINT, BIGINT, BIGINT, TEXT) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.verify_event_raffle_claim(BIGINT, BIGINT, BIGINT, BIGINT, TEXT) FROM PUBLIC, anon, authenticated;

GRANT EXECUTE ON FUNCTION public.create_event_raffle(BIGINT, BIGINT, TEXT, BIGINT, INTEGER, BIGINT) TO service_role;
GRANT EXECUTE ON FUNCTION public.manage_event_raffle_entry(BIGINT, BIGINT, BIGINT, BIGINT, TEXT, TEXT, TEXT) TO service_role;
GRANT EXECUTE ON FUNCTION public.lock_event_raffle(BIGINT, BIGINT, BIGINT) TO service_role;
GRANT EXECUTE ON FUNCTION public.prepare_event_raffle_beacon(BIGINT, BIGINT, BIGINT, TEXT, TEXT, BIGINT) TO service_role;
GRANT EXECUTE ON FUNCTION public.record_event_raffle_result(BIGINT, BIGINT, BIGINT, JSONB, TEXT, TEXT, BIGINT, TEXT, TEXT, JSONB) TO service_role;
GRANT EXECUTE ON FUNCTION public.cancel_event_raffle(BIGINT, BIGINT, BIGINT, TEXT) TO service_role;
GRANT EXECUTE ON FUNCTION public.set_event_raffle_lock_message(BIGINT, BIGINT, BIGINT) TO service_role;
GRANT EXECUTE ON FUNCTION public.set_event_raffle_result_message(BIGINT, BIGINT, BIGINT) TO service_role;
GRANT EXECUTE ON FUNCTION public.set_event_raffle_spin_message(BIGINT, BIGINT, BIGINT) TO service_role;
GRANT EXECUTE ON FUNCTION public.issue_event_raffle_claim(BIGINT, BIGINT, BIGINT, BIGINT, TEXT) TO service_role;
GRANT EXECUTE ON FUNCTION public.verify_event_raffle_claim(BIGINT, BIGINT, BIGINT, BIGINT, TEXT) TO service_role;
