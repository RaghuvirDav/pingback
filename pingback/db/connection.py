from __future__ import annotations

import aiosqlite

from pingback.config import DB_PATH
from pingback.db.schema import initialize_database

_db: aiosqlite.Connection | None = None


async def get_database() -> aiosqlite.Connection:
    global _db
    if _db is None:
        _db = await aiosqlite.connect(DB_PATH)
        _db.row_factory = aiosqlite.Row
        await initialize_database(_db)
    return _db


async def close_database() -> None:
    global _db
    if _db is not None:
        await _db.close()
        _db = None
