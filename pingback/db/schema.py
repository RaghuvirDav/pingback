import aiosqlite


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    email TEXT NOT NULL UNIQUE,
    name TEXT,
    plan TEXT NOT NULL DEFAULT 'free',
    api_key TEXT UNIQUE,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS monitors (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    name TEXT NOT NULL,
    url TEXT NOT NULL,
    interval_seconds INTEGER NOT NULL DEFAULT 300,
    status TEXT NOT NULL DEFAULT 'active',
    is_public INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS check_results (
    id TEXT PRIMARY KEY,
    monitor_id TEXT NOT NULL,
    status TEXT NOT NULL,
    status_code INTEGER,
    response_time_ms INTEGER,
    error TEXT,
    checked_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (monitor_id) REFERENCES monitors(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS audit_log (
    id TEXT PRIMARY KEY,
    user_id TEXT,
    action TEXT NOT NULL,
    resource_type TEXT,
    resource_id TEXT,
    ip_address TEXT,
    detail TEXT,
    timestamp TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_monitors_user_id ON monitors(user_id);
CREATE INDEX IF NOT EXISTS idx_monitors_status ON monitors(status);
CREATE INDEX IF NOT EXISTS idx_check_results_monitor_id ON check_results(monitor_id);
CREATE INDEX IF NOT EXISTS idx_check_results_checked_at ON check_results(checked_at);

-- MAK-147: pre-aggregated rollups so dashboard reads don't scan raw check_results.
-- window_start is ISO8601 UTC at the bucket boundary (e.g. ...:42:00Z for the 1m
-- bucket starting at 14:42:00). PRIMARY KEY enforces idempotent recompaction via
-- INSERT OR REPLACE.
CREATE TABLE IF NOT EXISTS check_results_1m (
    monitor_id TEXT NOT NULL,
    window_start TEXT NOT NULL,
    check_count INTEGER NOT NULL,
    ok_count INTEGER NOT NULL,
    fail_count INTEGER NOT NULL,
    avg_latency_ms REAL,
    p50_latency_ms INTEGER,
    p95_latency_ms INTEGER,
    p99_latency_ms INTEGER,
    PRIMARY KEY (monitor_id, window_start),
    FOREIGN KEY (monitor_id) REFERENCES monitors(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_check_results_1m_window ON check_results_1m(window_start);

CREATE TABLE IF NOT EXISTS check_results_5m (
    monitor_id TEXT NOT NULL,
    window_start TEXT NOT NULL,
    check_count INTEGER NOT NULL,
    ok_count INTEGER NOT NULL,
    fail_count INTEGER NOT NULL,
    avg_latency_ms REAL,
    p50_latency_ms INTEGER,
    p95_latency_ms INTEGER,
    p99_latency_ms INTEGER,
    PRIMARY KEY (monitor_id, window_start),
    FOREIGN KEY (monitor_id) REFERENCES monitors(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_check_results_5m_window ON check_results_5m(window_start);

CREATE TABLE IF NOT EXISTS check_results_1h (
    monitor_id TEXT NOT NULL,
    window_start TEXT NOT NULL,
    check_count INTEGER NOT NULL,
    ok_count INTEGER NOT NULL,
    fail_count INTEGER NOT NULL,
    avg_latency_ms REAL,
    p50_latency_ms INTEGER,
    p95_latency_ms INTEGER,
    p99_latency_ms INTEGER,
    PRIMARY KEY (monitor_id, window_start),
    FOREIGN KEY (monitor_id) REFERENCES monitors(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_check_results_1h_window ON check_results_1h(window_start);
CREATE INDEX IF NOT EXISTS idx_audit_log_user_id ON audit_log(user_id);
CREATE INDEX IF NOT EXISTS idx_audit_log_timestamp ON audit_log(timestamp);
CREATE INDEX IF NOT EXISTS idx_audit_log_action ON audit_log(action);

CREATE TABLE IF NOT EXISTS notification_channels (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    type TEXT NOT NULL, -- 'slack', 'teams', 'telegram', 'whatsapp'
    config TEXT NOT NULL, -- JSON blob of encrypted config
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_notification_channels_user_id ON notification_channels(user_id);
CREATE INDEX IF NOT EXISTS idx_notification_channels_enabled ON notification_channels(enabled);
"""


DIGEST_PREFS_SQL = """
CREATE TABLE IF NOT EXISTS digest_preferences (
    user_id TEXT PRIMARY KEY,
    enabled INTEGER NOT NULL DEFAULT 1,
    send_hour_utc INTEGER NOT NULL DEFAULT 8,
    unsubscribe_token TEXT NOT NULL,
    last_sent_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_digest_prefs_enabled ON digest_preferences(enabled);
CREATE INDEX IF NOT EXISTS idx_digest_prefs_token ON digest_preferences(unsubscribe_token);
"""


MIGRATIONS = [
    # Add is_public column to monitors (idempotent)
    """ALTER TABLE monitors ADD COLUMN is_public INTEGER NOT NULL DEFAULT 0""",
    # Add consent_given_at column to users for GDPR consent tracking
    """ALTER TABLE users ADD COLUMN consent_given_at TEXT""",
    # Add api_key_hash for fast lookup of encrypted API keys
    """ALTER TABLE users ADD COLUMN api_key_hash TEXT""",
    # Track last login time for abandoned-account detection
    """ALTER TABLE users ADD COLUMN last_login_at TEXT""",
    # Stripe billing integration
    """ALTER TABLE users ADD COLUMN stripe_customer_id TEXT""",
    """ALTER TABLE users ADD COLUMN stripe_subscription_id TEXT""",
    # Deterministic email hash for signup-time dedup (Fernet encryption is
    # non-deterministic so a plain UNIQUE index on encrypted `email` does not
    # prevent duplicate signups).
    """ALTER TABLE users ADD COLUMN email_hash TEXT""",
    """CREATE UNIQUE INDEX IF NOT EXISTS idx_users_email_hash ON users(email_hash)""",
    # Subscription renewal timestamp (ISO 8601 UTC). Originally added for
    # Stripe; reused by the Paddle integration.
    """ALTER TABLE users ADD COLUMN plan_renews_at TEXT""",
    # Legacy Stripe webhook idempotency table — kept so existing rows aren't
    # lost. New events go to `paddle_events` below.
    """CREATE TABLE IF NOT EXISTS stripe_events (
        id TEXT PRIMARY KEY,
        type TEXT NOT NULL,
        received_at TEXT NOT NULL DEFAULT (datetime('now'))
    )""",
    # Email + password auth (MAK-96). Existing API-key auth continues to work
    # for programmatic access; these columns light up the UI sign-in flow.
    """ALTER TABLE users ADD COLUMN password_hash TEXT""",
    """ALTER TABLE users ADD COLUMN email_verified INTEGER NOT NULL DEFAULT 0""",
    """ALTER TABLE users ADD COLUMN verification_token TEXT""",
    """ALTER TABLE users ADD COLUMN verification_expires_at TEXT""",
    """ALTER TABLE users ADD COLUMN reset_token TEXT""",
    """ALTER TABLE users ADD COLUMN reset_expires_at TEXT""",
    """CREATE INDEX IF NOT EXISTS idx_users_verification_token ON users(verification_token)""",
    """CREATE INDEX IF NOT EXISTS idx_users_reset_token ON users(reset_token)""",
    # Trust the incumbents: any row that existed before this migration ran (no
    # password_hash yet) gets email_verified=1 so they can keep using the app
    # while they set a password on next login.
    """UPDATE users SET email_verified = 1 WHERE password_hash IS NULL""",
    # Paddle billing (MAK-82 pivot from Stripe — India-friendly Merchant of
    # Record). Stripe-named columns are left in place; a follow-up migration
    # will drop them once nothing reads them.
    """ALTER TABLE users ADD COLUMN paddle_customer_id TEXT""",
    """ALTER TABLE users ADD COLUMN paddle_subscription_id TEXT""",
    # When a Pro user cancels, Paddle keeps them on Pro until plan_cancel_at;
    # we honour that grace period before flipping plan='free'.
    """ALTER TABLE users ADD COLUMN plan_cancel_at TEXT""",
    """CREATE TABLE IF NOT EXISTS paddle_events (
        id TEXT PRIMARY KEY,
        type TEXT NOT NULL,
        received_at TEXT NOT NULL DEFAULT (datetime('now'))
    )""",
    # Per-user timezone for daily digest delivery (MAK-124). IANA name.
    # Defaulting to 'Etc/UTC' grandfathers existing users to the prior
    # UTC-relative behavior until they pick their own zone.
    """ALTER TABLE users ADD COLUMN timezone TEXT NOT NULL DEFAULT 'Etc/UTC'""",
    # Backfill consent for any user who already opted into the digest. The
    # opt-in toggle was the consent moment in practice — we just never
    # recorded it, which silently blocked every digest send.
    """UPDATE users SET consent_given_at = COALESCE(consent_given_at, datetime('now'))
       WHERE id IN (SELECT user_id FROM digest_preferences WHERE enabled = 1)""",
    # MAK-111: stamp the moment we sent the Pro welcome/receipt email so a
    # retried subscription.created (different event_id, same sub) doesn't
    # double-send. Webhook-level idempotency on event_id only catches exact
    # webhook retries, not Paddle re-emitting the same logical event.
    """ALTER TABLE users ADD COLUMN pro_welcome_sent_at TEXT""",
    # MAK-161: cache the last-known Paddle subscription status so the billing
    # page can render a past_due dunning banner without calling Paddle. plan
    # alone can't represent past_due — we keep the user on Pro during retries.
    """ALTER TABLE users ADD COLUMN paddle_subscription_status TEXT""",
]


async def initialize_database(db: aiosqlite.Connection) -> None:
    await db.execute("PRAGMA journal_mode = WAL")
    await db.execute("PRAGMA foreign_keys = ON")
    await db.executescript(SCHEMA_SQL)
    await db.executescript(DIGEST_PREFS_SQL)
    for migration in MIGRATIONS:
        try:
            await db.execute(migration)
        except Exception:
            pass  # Column already exists
    await db.commit()
