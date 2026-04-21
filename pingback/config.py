import os
from pathlib import Path

from dotenv import load_dotenv

# Load .env from project root (two levels up from this file).
_env_file = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(_env_file)

PORT = int(os.environ.get("PORT", "8000"))
HOST = os.environ.get("HOST", "0.0.0.0")
DB_PATH = os.environ.get("DB_PATH", "pingback.db")
DEFAULT_CHECK_INTERVAL = 300  # 5 minutes in seconds
CHECK_TIMEOUT_SECONDS = 30
MAX_MONITORS_FREE = 5
MAX_MONITORS_PRO: int | None = None  # unlimited
MAX_MONITORS_BUSINESS = 200

# Check intervals per plan (seconds).
CHECK_INTERVAL_FREE = 300      # 5 minutes
CHECK_INTERVAL_PRO = 60        # 1 minute

# Per-plan retention of historical check_results (days). Free keeps the last
# week only; Pro keeps 90 days so trend panels have meaningful history.
HISTORY_DAYS_FREE = 7
HISTORY_DAYS_PRO = 90

# Paddle billing configuration (MAK-97). Swapped in from the Stripe scaffold
# that was never activated in prod. Leave blank to run as single-tier free.
#   PADDLE_ENV=sandbox|production            — picks the Paddle.js environment.
#   PADDLE_API_KEY                           — server → Paddle API (unused today,
#                                              kept for future server calls).
#   PADDLE_CLIENT_SIDE_TOKEN                 — Paddle.Initialize({ token }) on
#                                              the billing/pricing pages.
#   PADDLE_NOTIFICATION_SECRET               — HMAC secret for webhook signature
#                                              verification (Paddle-Signature).
#   PADDLE_PRICE_ID_PRO_{MONTHLY,ANNUAL}     — Pro tier price ids from the
#                                              Paddle product catalog.
PADDLE_ENV = os.environ.get("PADDLE_ENV", "sandbox")
PADDLE_API_KEY = os.environ.get("PADDLE_API_KEY", "")
PADDLE_CLIENT_SIDE_TOKEN = os.environ.get("PADDLE_CLIENT_SIDE_TOKEN", "")
PADDLE_NOTIFICATION_SECRET = os.environ.get("PADDLE_NOTIFICATION_SECRET", "")
PADDLE_PRICE_ID_PRO_MONTHLY = os.environ.get("PADDLE_PRICE_ID_PRO_MONTHLY", "")
PADDLE_PRICE_ID_PRO_ANNUAL = os.environ.get("PADDLE_PRICE_ID_PRO_ANNUAL", "")


def paddle_template_context() -> dict:
    """The subset of Paddle config any page rendering the upgrade CTA needs."""
    return {
        "paddle_client_side_token": PADDLE_CLIENT_SIDE_TOKEN,
        "paddle_env": PADDLE_ENV,
        "paddle_price_id_pro_monthly": PADDLE_PRICE_ID_PRO_MONTHLY,
    }

# Encryption key for sensitive fields (Fernet symmetric encryption).
# Generate with: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
ENCRYPTION_KEY = os.environ.get("ENCRYPTION_KEY", "")

# Data retention: number of days to keep check_results before purging.
RETENTION_DAYS = int(os.environ.get("RETENTION_DAYS", "90"))

# Days of login inactivity before a free-tier account is considered abandoned.
# Monitors are paused and check history is deleted to reclaim resources.
ABANDONED_ACCOUNT_DAYS = int(os.environ.get("ABANDONED_ACCOUNT_DAYS", "30"))

# Set to "production" to enforce HTTPS redirection.
APP_ENV = os.environ.get("APP_ENV", "development")

# Resend API key for transactional emails (daily digest, etc.).
# Sign up at https://resend.com and grab your API key.
RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")

# The verified sender address used for outgoing emails.
RESEND_FROM_EMAIL = os.environ.get("RESEND_FROM_EMAIL", "Pingback <noreply@example.com>")

# Base URL for links in emails (unsubscribe, dashboard).
APP_BASE_URL = os.environ.get("APP_BASE_URL", "http://localhost:8000")

# AWS credentials — loaded from .env or environment.
# Used for SES, S3, and other AWS services (free tier only).
AWS_ACCESS_KEY_ID = os.environ.get("AWS_ACCESS_KEY_ID", "")
AWS_SECRET_ACCESS_KEY = os.environ.get("AWS_SECRET_ACCESS_KEY", "")
AWS_DEFAULT_REGION = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")

# Sentry error tracking (free tier — 5k events/mo).
# Leave SENTRY_DSN empty to disable. No-op safe for local dev.
SENTRY_DSN = os.environ.get("SENTRY_DSN", "")
SENTRY_TRACES_SAMPLE_RATE = float(os.environ.get("SENTRY_TRACES_SAMPLE_RATE", "0.1"))
SENTRY_ENVIRONMENT = os.environ.get("SENTRY_ENVIRONMENT", APP_ENV)
SENTRY_RELEASE = os.environ.get("SENTRY_RELEASE", "")

# Enables the /debug/boom route so we can verify Sentry wiring end-to-end.
# Never enable in prod outside a smoke test.
DEBUG_BOOM_ENABLED = os.environ.get("DEBUG_BOOM_ENABLED", "").lower() in (
    "1",
    "true",
    "yes",
    "on",
)
