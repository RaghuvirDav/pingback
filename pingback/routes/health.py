from fastapi import APIRouter

router = APIRouter()


@router.get("/health")
async def health_check():
    return {"status": "ok", "service": "pingback", "version": "0.1.0"}


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
