CREATE TABLE IF NOT EXISTS subscriptions (
    customer_id text PRIMARY KEY,
    panel_uuid uuid UNIQUE NOT NULL DEFAULT gen_random_uuid(),
    valid_through date NOT NULL,
    quota_gb integer NOT NULL CHECK (quota_gb > 0),
    enabled boolean NOT NULL DEFAULT true,
    version bigint NOT NULL DEFAULT 1,
    applied_version bigint NOT NULL DEFAULT 0,
    next_attempt timestamptz NOT NULL DEFAULT now(),
    failures integer NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS subscriptions_pending
    ON subscriptions (next_attempt) WHERE applied_version < version;
CREATE INDEX IF NOT EXISTS subscriptions_expiry
    ON subscriptions (valid_through) WHERE enabled;
