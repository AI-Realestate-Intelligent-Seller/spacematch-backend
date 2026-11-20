"""Configuration and local development secrets for the standalone Reels MVP."""
from __future__ import annotations

import json
import os
import secrets
from pathlib import Path
from urllib.parse import urlparse

from cryptography.fernet import Fernet
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]  # project root: .env, data/, .dev-keys.json
load_dotenv(ROOT / ".env")

# Vercel runs the app as serverless functions: read-only files, no long-lived background threads.
ON_VERCEL = bool(os.getenv("VERCEL"))
_vercel_url = os.getenv("VERCEL_PROJECT_PRODUCTION_URL") or os.getenv("VERCEL_URL")
BASE_URL = (os.getenv("BASE_URL") or (f"https://{_vercel_url}" if ON_VERCEL and _vercel_url else "")
            or "http://127.0.0.1:8000").rstrip("/")

DB_PATH = Path(os.getenv("DB_PATH", str(ROOT / "data" / "spacematch_reels.db")))
if not DB_PATH.is_absolute():
    DB_PATH = ROOT / DB_PATH



def _database_url() -> str:
    """Postgres connection string (e.g. Supabase's Session pooler). Empty -> SQLite at DB_PATH."""
    url = os.getenv("DATABASE_URL", "").strip()
    if url and os.getenv("DATABASE_SSL", "true").lower() == "true" and "sslmode=" not in url:
        url += ("&" if "?" in url else "?") + "sslmode=require"
    return url


DATABASE_URL = _database_url()
if ON_VERCEL and not DATABASE_URL:
    raise RuntimeError("Set DATABASE_URL (Supabase Session pooler) in Vercel: its disk is read-only and "
                       "temporary, so the SQLite file can't be used there")
DATABASE_POOL_MAX = int(os.getenv("DATABASE_POOL_MAX", "5"))
DATABASE_LOGGING = os.getenv("DATABASE_LOGGING", "false").lower() == "true"

HOST = urlparse(BASE_URL).hostname or "127.0.0.1"
IS_LOCAL = HOST in {"localhost", "127.0.0.1", "::1"}
INVITE_TTL_HOURS = int(os.getenv("INVITE_TTL_HOURS", "72"))

# Hub API (/v1)
# Background refresh queue + daily schedule. Only ONE server should run them against a shared database:
# default ON for a deployed BASE_URL, OFF on localhost (your laptop) and on Vercel (serverless functions
# are frozen between requests, so a background thread can't run there). REFRESH_WORKER=on/off overrides.
_worker = os.getenv("REFRESH_WORKER", "").strip().lower()
REFRESH_WORKER = (not IS_LOCAL and not ON_VERCEL) if not _worker else _worker not in {"off", "false", "0", "no"}
DAILY_REFRESH_ENABLED = os.getenv("DAILY_REFRESH_ENABLED", "true").lower() == "true"
DAILY_REFRESH_TIME = os.getenv("DAILY_REFRESH_TIME", "07:00")          # local time in DAILY_REFRESH_TZ
DAILY_REFRESH_TZ = os.getenv("DAILY_REFRESH_TZ", "America/Chicago")    # follows daylight saving
MANUAL_REFRESH_COOLDOWN_S = int(os.getenv("MANUAL_REFRESH_COOLDOWN_S", "600"))  # per property
API_RATE_LIMIT_PER_MIN = int(os.getenv("API_RATE_LIMIT_PER_MIN", "120"))        # per API key

# Instagram via Apify's Instagram Reel Scraper (runs on Apify's proxies). Used when APIFY_TOKEN is set;
# the local browser is the fallback.
APIFY_TOKEN = os.getenv("APIFY_TOKEN", "").strip()
APIFY_REEL_ACTOR = os.getenv("APIFY_REEL_ACTOR", "apify~instagram-reel-scraper")
APIFY_TIMEOUT_S = int(os.getenv("APIFY_TIMEOUT_S", "240"))

# YouTube via the official YouTube Data API v3 (plain Google Cloud API key). Browser is the fallback.
YOUTUBE_API_KEY = os.getenv("YOUTUBE_API_KEY", "").strip()

# Outgoing email (broker invites). Leave SMTP_HOST empty to print emails to the console instead.
SMTP_HOST = os.getenv("SMTP_HOST", "").strip()
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER") or os.getenv("SMTP_USERNAME", "")          # either name works
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
SMTP_FROM = os.getenv("SMTP_FROM") or os.getenv("EMAIL_FROM") or SMTP_USER or "no-reply@localhost"
SMTP_SECURITY = os.getenv("SMTP_SECURITY", "starttls").lower()  # starttls | ssl | none
SMTP_TIMEOUT_S = int(os.getenv("SMTP_TIMEOUT_S", "10"))



def local_key(key_name: str, create_value) -> str:
    """For localhost development only; production MUST provide env secrets."""
    v = os.getenv(key_name)
    if v:
        return v
    if not IS_LOCAL:
        raise RuntimeError(f"Set {key_name} before deploying off localhost"
                           + (" (Vercel: Project Settings -> Environment Variables, then redeploy)" if ON_VERCEL else ""))
    path = ROOT / ".dev-keys.json"
    stored = json.loads(path.read_text()) if path.exists() else {}
    if key_name not in stored:
        stored[key_name] = create_value()
        path.write_text(json.dumps(stored, indent=2))
        path.chmod(0o600)
    return stored[key_name]


SESSION_SECRET = local_key("SESSION_SECRET", lambda: secrets.token_urlsafe(48))
TOKEN_KEY = local_key("TOKEN_ENCRYPTION_KEY", lambda: Fernet.generate_key().decode())
FERNET = Fernet(TOKEN_KEY.encode())


def encrypt_token(token: str) -> str:
    return FERNET.encrypt(token.encode()).decode()


def decrypt_token(encrypted: str) -> str:
    return FERNET.decrypt(encrypted.encode()).decode()
