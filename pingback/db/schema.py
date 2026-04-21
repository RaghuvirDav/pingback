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
CREATE INDEX IF NOT EXISTS idx_audit_log_user_id ON audit_log(user_id);
CREATE INDEX IF NOT EXISTS idx_audit_log_timestamp ON audit_log(timestamp);
CREATE INDEX IF NOT EXISTS idx_audit_log_action ON audit_log(action);
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
    # Subscription renewal timestamp (ISO 8601 UTC) — provider-agnostic.
    """ALTER TABLE users ADD COLUMN plan_renews_at TEXT""",
    # Paddle billing integration (MAK-97). Replaces the never-activated Stripe
    # scaffold from MAK-82. Paddle returns a per-subscription `customer_portal_url`
    # on creation — cache it on the user row so GET /dashboard/billing/portal
    # can 302 straight to it without hitting the Paddle API on every click.
    """ALTER TABLE users ADD COLUMN paddle_customer_id TEXT""",
    """ALTER TABLE users ADD COLUMN paddle_subscription_id TEXT""",
    """ALTER TABLE users ADD COLUMN paddle_portal_url TEXT""",
    # Idempotency log for Paddle webhook events. Paddle retries deliver the
    # same event_id, so we record each processed id and reject duplicates.
    """CREATE TABLE IF NOT EXISTS paddle_events (
        id TEXT PRIMARY KEY,
        type TEXT NOT NULL,
        received_at TEXT NOT NULL DEFAULT (datetime('now'))
    )""",
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
