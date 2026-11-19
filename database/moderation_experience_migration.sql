-- Moderation experience: audit trail and reusable staff reply templates

CREATE TABLE IF NOT EXISTS public.moderation_audit_log (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    guild_id BIGINT NOT NULL,
    actor_id BIGINT NOT NULL,
    action TEXT NOT NULL,
    target_type TEXT NOT NULL,
    target_id BIGINT,
    details JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_moderation_audit_guild
    ON public.moderation_audit_log(guild_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_moderation_audit_actor
    ON public.moderation_audit_log(guild_id, actor_id, created_at DESC);

CREATE TABLE IF NOT EXISTS public.moderation_reply_templates (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    guild_id BIGINT NOT NULL,
    created_by BIGINT NOT NULL,
    action_type TEXT NOT NULL CHECK (action_type IN ('APPROVE', 'REJECT', 'CHANGE_REQUEST')),
    name TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(guild_id, action_type, name)
);

CREATE INDEX IF NOT EXISTS idx_moderation_templates
    ON public.moderation_reply_templates(guild_id, action_type, name);

ALTER TABLE public.moderation_audit_log ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.moderation_reply_templates ENABLE ROW LEVEL SECURITY;
