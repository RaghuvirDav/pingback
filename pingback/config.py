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

# Paddle billing configuration (MAK-82 pivoted from Stripe to Paddle MoR).
# All values come from the Paddle Dashboard; PADDLE_ENVIRONMENT toggles the
# API base URL between sandbox.paddle.com and api.paddle.com.
PADDLE_ENVIRONMENT = os.environ.get("PADDLE_ENVIRONMENT", "sandbox")
PADDLE_API_KEY = os.environ.get("PADDLE_API_KEY", "")
PADDLE_CLIENT_TOKEN = os.environ.get("PADDLE_CLIENT_TOKEN", "")
PADDLE_WEBHOOK_SECRET = os.environ.get("PADDLE_WEBHOOK_SECRET", "")
PADDLE_PRODUCT_ID = os.environ.get("PADDLE_PRODUCT_ID", "")
PADDLE_PRICE_ID_MONTHLY = os.environ.get("PADDLE_PRICE_ID_MONTHLY", "")
PADDLE_PRICE_ID_YEARLY = os.environ.get("PADDLE_PRICE_ID_YEARLY", "")
PADDLE_DISCOUNT_ID_LAUNCH = os.environ.get("PADDLE_DISCOUNT_ID_LAUNCH", "")

PADDLE_API_BASE_URL = (
    "https://sandbox-api.paddle.com"
    if PADDLE_ENVIRONMENT == "sandbox"
    else "https://api.paddle.com"
)

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

# Resend API key for transactional emails (daily digest, verification, etc.).
# Sign up at https://resend.com and grab your API key.
RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")

# Sender identities. `hello@` is deliberately NOT used for outbound — it is
# the inbound-only address for human-to-human conversation (forwarded via
# Namecheap). `daily_status@` sends the daily digest; `noreply@` sends
# account mail (verification, password reset, billing receipts).
# RESEND_FROM_EMAIL is the legacy single-sender fallback used when the more
# specific vars are blank.
RESEND_FROM_EMAIL = os.environ.get("RESEND_FROM_EMAIL", "Pingback <noreply@example.com>")
EMAIL_FROM_DAILY_STATUS = os.environ.get("EMAIL_FROM_DAILY_STATUS", "") or RESEND_FROM_EMAIL
EMAIL_FROM_NOREPLY = os.environ.get("EMAIL_FROM_NOREPLY", "") or RESEND_FROM_EMAIL

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
