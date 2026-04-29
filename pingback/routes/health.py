from fastapi import APIRouter
from fastapi.responses import JSONResponse

from pingback.db.connection import get_database
from pingback.version import VERSION

router = APIRouter()


@router.get("/health")
async def health_check():
    return {"status": "ok", "service": "pingback", "version": "0.1.0"}


@router.get("/healthz")
async def healthz():
    """Liveness/readiness probe used by deploy scripts and uptime monitors.

    Hits the DB with a cheap ``SELECT 1`` so a wedged sqlite handle fails
    the probe instead of falsely reporting healthy. Returns the running
    build's git sha so we can confirm a deploy actually swapped the symlink.
    """
    try:
        db = await get_database()
        async with db.execute("SELECT 1") as cur:
            row = await cur.fetchone()
        ok = row is not None and row[0] == 1
    except Exception:
        return JSONResponse(
            {"ok": False, "version": VERSION, "db": "unreachable"},
            status_code=503,
        )
    return {"ok": ok, "version": VERSION}


@router.get("/api/privacy-policy")
async def privacy_policy():
    return {
        "title": "Pingback Privacy Policy",
        "effective_date": "2026-04-19",
        "data_controller": "Pingback",
        "policy": {
            "data_collected": [
                "Email address and name (provided at registration)",
                "Monitor URLs and configuration",
                "Health-check results (status codes, response times, errors)",
            ],
            "purpose": "We collect and process data solely to provide uptime monitoring services.",
            "legal_basis": "Processing is based on your explicit consent, recorded at registration.",
            "data_retention": "Your data is retained for as long as your account is active. You may request deletion at any time.",
            "your_rights": [
                "Right to access: GET /api/users/{user_id}/export",
                "Right to erasure: DELETE /api/users/{user_id}",
                "Right to withdraw consent: POST /api/users/{user_id}/consent",
                "Right to data portability: GET /api/users/{user_id}/export",
            ],
            "data_sharing": "We do not sell or share your personal data with third parties.",
            "contact": "For privacy inquiries, contact the Pingback data protection team.",
        },
    }
