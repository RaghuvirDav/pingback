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
MAX_MONITORS_FREE = 3
MAX_MONITORS_PRO = 50
MAX_MONITORS_BUSINESS = 200

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
RESEND_FROM_EMAIL = os.environ.get("RESEND_FROM_EMAIL", "Pingback <noreply@pingback.dev>")

# Base URL for links in emails (unsubscribe, dashboard).
APP_BASE_URL = os.environ.get("APP_BASE_URL", "http://localhost:8000")

# AWS credentials — loaded from .env or environment.
# Used for SES, S3, and other AWS services (free tier only).
AWS_ACCESS_KEY_ID = os.environ.get("AWS_ACCESS_KEY_ID", "")
AWS_SECRET_ACCESS_KEY = os.environ.get("AWS_SECRET_ACCESS_KEY", "")
AWS_DEFAULT_REGION = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
