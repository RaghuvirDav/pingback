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
    # Stripe subscription renewal timestamp (ISO 8601 UTC).
    """ALTER TABLE users ADD COLUMN plan_renews_at TEXT""",
    # Idempotency log for Stripe webhook events. Retries deliver the same
    # event id, so we record each processed id and reject duplicates.
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
