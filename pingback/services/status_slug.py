"""Status-page slug generation, validation, and backfill (MAK-163)."""
from __future__ import annotations

import re
import unicodedata

import aiosqlite

# 3 chars is short enough to feel like a real choice; 32 keeps URLs reasonable.
SLUG_MIN_LEN = 3
SLUG_MAX_LEN = 32

# Slug alphabet is what survives a `slugify` pass: lowercase alphanumerics +
# single hyphens, no leading/trailing hyphens.
_SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")

# Paths the public status router would shadow. Anyone claiming these would
# either break the app (`admin`, `api`) or look like phishing (`pingback`).
RESERVED_SLUGS = frozenset({
    "admin", "api", "app", "billing", "dashboard", "debug", "digest",
    "favicon", "health", "healthz", "login", "logout", "monitor", "monitors",
    "pingback", "pricing", "privacy", "refund", "reset-password",
    "settings", "signup", "static", "status", "support", "terms", "verify",
})


def slugify(text: str) -> str:
    """Convert arbitrary text to a status-page slug.

    Returns "" if nothing usable remains. Caller must supply a fallback.
    """
    if not text:
        return ""
    # Strip diacritics: "Café" → "Cafe"
    normalised = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    cleaned = re.sub(r"[^a-z0-9]+", "-", normalised.lower()).strip("-")
    return cleaned[:SLUG_MAX_LEN]


def validate_slug(candidate: str) -> str | None:
    """Return None if `candidate` is acceptable, otherwise an error message."""
    if not candidate:
        return "Pick a slug."
    if len(candidate) < SLUG_MIN_LEN:
        return f"Slug must be at least {SLUG_MIN_LEN} characters."
    if len(candidate) > SLUG_MAX_LEN:
        return f"Slug must be at most {SLUG_MAX_LEN} characters."
    if not _SLUG_RE.match(candidate):
        return "Use lowercase letters, numbers and single hyphens only."
    if candidate in RESERVED_SLUGS:
        return "That slug is reserved. Pick another."
    return None


def _seed_for(name: str | None, email: str | None, user_id: str) -> str:
    """Best-effort seed slug from name → email-local → user_id prefix."""
    for source in (name, email.split("@", 1)[0] if email else None):
        seed = slugify(source or "")
        if len(seed) >= SLUG_MIN_LEN:
            return seed
    # No human-readable input survived: fall back to a stable id-derived slug.
    return f"page-{user_id.replace('-', '')[:8]}"


async def _slug_taken(db: aiosqlite.Connection, slug: str, except_user_id: str | None = None) -> bool:
    if except_user_id is None:
        async with db.execute(
            "SELECT 1 FROM users WHERE status_page_slug = ?", (slug,),
        ) as cur:
            return await cur.fetchone() is not None
    async with db.execute(
        "SELECT 1 FROM users WHERE status_page_slug = ? AND id <> ?",
        (slug, except_user_id),
    ) as cur:
        return await cur.fetchone() is not None


async def generate_unique_slug(
    db: aiosqlite.Connection,
    *,
    name: str | None,
    email: str | None,
    user_id: str,
) -> str:
    """Return a slug guaranteed to satisfy validate_slug + DB UNIQUE.

    Tries the seed first, then `seed-<6hex of user_id>`, then progressively
    longer id suffixes. The id-derived fallback always succeeds.
    """
    seed = _seed_for(name, email, user_id)
    # Reserved seeds (e.g. user named "Settings") get nudged to "settings-page"
    # so the auto-generated value is still readable.
    if seed in RESERVED_SLUGS:
        seed = f"{seed}-page"

    candidates = [seed]
    id_clean = user_id.replace("-", "")
    for n in (6, 8, 12, 24):
        candidates.append(f"{seed[: SLUG_MAX_LEN - n - 1]}-{id_clean[:n]}".strip("-"))

    for candidate in candidates:
        candidate = candidate[:SLUG_MAX_LEN].strip("-")
        if validate_slug(candidate) is None and not await _slug_taken(db, candidate):
            return candidate

    # Pathological collision (shouldn't happen — UUIDs are 122 random bits).
    return f"page-{id_clean[:24]}"


async def assign_slug_if_missing(
    db: aiosqlite.Connection,
    *,
    user_id: str,
    name: str | None,
    email: str | None,
) -> str:
    """Set status_page_slug for one user iff it's currently NULL. Returns the
    final slug (existing or newly assigned)."""
    async with db.execute(
        "SELECT status_page_slug FROM users WHERE id = ?", (user_id,),
    ) as cur:
        row = await cur.fetchone()
    if row and row["status_page_slug"]:
        return row["status_page_slug"]

    slug = await generate_unique_slug(db, name=name, email=email, user_id=user_id)
    await db.execute(
        "UPDATE users SET status_page_slug = ? WHERE id = ? AND status_page_slug IS NULL",
        (slug, user_id),
    )
    await db.commit()
    return slug


async def backfill_status_slugs(db: aiosqlite.Connection) -> int:
    """Assign a slug to every existing user that doesn't have one yet.

    Returns count of users updated. Idempotent: safe to call on every startup.
    Email is encrypted in the DB but we only need it as a slug seed when name
    is empty — and only for users we are creating slugs for, not the whole
    table. Decrypting per-user keeps this loop O(missing_slugs).
    """
    from pingback.encryption import decrypt_value  # avoid import cycles

    async with db.execute(
        "SELECT id, name, email FROM users WHERE status_page_slug IS NULL"
    ) as cur:
        rows = await cur.fetchall()

    updated = 0
    for row in rows:
        try:
            email = decrypt_value(row["email"]) if row["email"] else None
        except Exception:
            email = None
        slug = await generate_unique_slug(
            db, name=row["name"], email=email, user_id=row["id"],
        )
        await db.execute(
            "UPDATE users SET status_page_slug = ? WHERE id = ? AND status_page_slug IS NULL",
            (slug, row["id"]),
        )
        updated += 1
    if updated:
        await db.commit()
    return updated
