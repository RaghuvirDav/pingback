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

CREATE INDEX IF NOT EXISTS idx_monitors_user_id ON monitors(user_id);
CREATE INDEX IF NOT EXISTS idx_monitors_status ON monitors(status);
CREATE INDEX IF NOT EXISTS idx_check_results_monitor_id ON check_results(monitor_id);
CREATE INDEX IF NOT EXISTS idx_check_results_checked_at ON check_results(checked_at);
"""


MIGRATIONS = [
    # Add is_public column to monitors (idempotent)
    """ALTER TABLE monitors ADD COLUMN is_public INTEGER NOT NULL DEFAULT 0""",
    # Add consent_given_at column to users for GDPR consent tracking
    """ALTER TABLE users ADD COLUMN consent_given_at TEXT""",
]


async def initialize_database(db: aiosqlite.Connection) -> None:
    await db.execute("PRAGMA journal_mode = WAL")
    await db.execute("PRAGMA foreign_keys = ON")
    await db.executescript(SCHEMA_SQL)
    for migration in MIGRATIONS:
        try:
            await db.execute(migration)
        except Exception:
            pass  # Column already exists
    await db.commit()
