-- Dashboard-configured recruitment review destinations.
-- Run once in the Supabase SQL Editor after the recruitment migrations.

CREATE TABLE IF NOT EXISTS public.recruitment_review_channels (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    guild_id BIGINT NOT NULL,
    slot SMALLINT NOT NULL CHECK (slot BETWEEN 1 AND 3),
    channel_id BIGINT NOT NULL,
    configured_by BIGINT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (guild_id, slot),
    UNIQUE (guild_id, channel_id)
);

CREATE INDEX IF NOT EXISTS idx_recruitment_review_channels_guild
    ON public.recruitment_review_channels (guild_id, slot);

-- Replace all configured destinations in one transaction so a partial write
-- cannot leave the dashboard with a half-updated channel list.
CREATE OR REPLACE FUNCTION public.set_recruitment_review_channels(
    p_guild_id BIGINT, p_channel_ids BIGINT[], p_configured_by BIGINT
) RETURNS JSONB LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
DECLARE v_channel BIGINT; v_slot INTEGER := 0;
BEGIN
    IF p_channel_ids IS NULL OR cardinality(p_channel_ids) < 1 OR cardinality(p_channel_ids) > 3 THEN
        RAISE EXCEPTION 'Choose between one and three review channels';
    END IF;
    IF cardinality(p_channel_ids) <> (SELECT count(DISTINCT channel_id) FROM unnest(p_channel_ids) AS ids(channel_id)) THEN
        RAISE EXCEPTION 'Review channels must be unique';
    END IF;
    DELETE FROM public.recruitment_review_channels WHERE guild_id = p_guild_id;
    FOREACH v_channel IN ARRAY p_channel_ids LOOP
        v_slot := v_slot + 1;
        INSERT INTO public.recruitment_review_channels(guild_id, slot, channel_id, configured_by)
        VALUES (p_guild_id, v_slot, v_channel, p_configured_by);
    END LOOP;
    RETURN jsonb_build_object('saved', v_slot);
END;
$$;
REVOKE ALL ON FUNCTION public.set_recruitment_review_channels(BIGINT, BIGINT[], BIGINT) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.set_recruitment_review_channels(BIGINT, BIGINT[], BIGINT) TO service_role;

ALTER TABLE public.recruitment_review_channels ENABLE ROW LEVEL SECURITY;
