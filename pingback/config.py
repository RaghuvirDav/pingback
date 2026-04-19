import os


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

# Set to "production" to enforce HTTPS redirection.
APP_ENV = os.environ.get("APP_ENV", "development")
