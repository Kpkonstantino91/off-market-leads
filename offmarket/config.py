"""Settings read from environment variables (see .env.example).

Things a broker changes day to day (price floor, neighborhoods, auto skip-trace) live in the
database and are edited on the website's Settings page; see `settings.py`.
"""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _load_dotenv():
    env = BASE_DIR / ".env"
    if not env.exists():
        return
    for line in env.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_dotenv()

# Tolerate common paste slips: blank values, surrounding quotes, or Neon's "psql '...'" copy format.
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
if DATABASE_URL.startswith("psql "):
    DATABASE_URL = DATABASE_URL[5:].strip()
DATABASE_URL = DATABASE_URL.strip("'\"") or f"sqlite:///{BASE_DIR / 'data' / 'offmarket.db'}"
# Use the psycopg 3 driver whichever way the Postgres URL is written.
for _prefix in ("postgres://", "postgresql://"):
    if DATABASE_URL.startswith(_prefix):
        DATABASE_URL = "postgresql+psycopg://" + DATABASE_URL[len(_prefix):]

# Daily schedule for the built-in scheduler (America/New_York).
DAILY_RUN_HOUR = int(os.getenv("DAILY_RUN_HOUR", "6"))
ENABLE_SCHEDULER = os.getenv("ENABLE_SCHEDULER", "1") == "1"

# Website login. If unset the site is open (fine on your laptop, not on the internet).
DASHBOARD_USER = os.getenv("DASHBOARD_USER", "broker")
DASHBOARD_PASSWORD = os.getenv("DASHBOARD_PASSWORD", "")

# Free NYC open data (ACRIS, DOF sales) and NY State (corporations). An app token raises rate limits.
SOCRATA_APP_TOKEN = os.getenv("SOCRATA_APP_TOKEN", "")

# --- Listing feed: any RESO Web API (REBNY RLS via Trestle, OneKey MLS, Bridge, MLS Grid) ---
# Either a static bearer token, or OAuth2 client credentials (Trestle uses client credentials).
RESO_BASE_URL = os.getenv("RESO_BASE_URL", "")            # e.g. https://api-trestle.corelogic.com/trestle/odata
RESO_TOKEN_URL = os.getenv("RESO_TOKEN_URL", "")          # e.g. https://api-trestle.corelogic.com/trestle/oidc/connect/token
RESO_CLIENT_ID = os.getenv("RESO_CLIENT_ID", "")
RESO_CLIENT_SECRET = os.getenv("RESO_CLIENT_SECRET", "")
RESO_SCOPE = os.getenv("RESO_SCOPE", "api")
RESO_ACCESS_TOKEN = os.getenv("RESO_ACCESS_TOKEN", "")
RESO_FEED_NAME = os.getenv("RESO_FEED_NAME", "REBNY RLS")
# Extra OData filter appended to every query, if the feed needs one (e.g. "OriginatingSystemName eq 'REBNY'").
RESO_EXTRA_FILTER = os.getenv("RESO_EXTRA_FILTER", "")

# --- Listing data without RLS: RentCast API (rentcast.io). Free plan = 50 requests/month. ---
RENTCAST_API_KEY = os.getenv("RENTCAST_API_KEY", "")
# One circle around Manhattan + brownstone Brooklyn; listings outside your areas are dropped afterwards.
RENTCAST_LAT = float(os.getenv("RENTCAST_LAT", "40.76"))
RENTCAST_LON = float(os.getenv("RENTCAST_LON", "-73.97"))
RENTCAST_RADIUS = float(os.getenv("RENTCAST_RADIUS", "8.5"))
RENTCAST_PAGES = int(os.getenv("RENTCAST_PAGES", "1"))   # requests per daily run
# Listings per request (max 500). Big pages over the whole area can time out, so stay moderate.
RENTCAST_PAGE_SIZE = int(os.getenv("RENTCAST_PAGE_SIZE", "250"))
# Hard stop: never make more than this many RentCast requests in a calendar month (free plan = 50).
RENTCAST_MONTHLY_CAP = int(os.getenv("RENTCAST_MONTHLY_CAP", "45"))

# Time limit for the daily sold check / owner lookup; whatever's left continues the next day.
SALE_CHECK_MINUTES = int(os.getenv("SALE_CHECK_MINUTES", "40"))

# --- Contact lookup (skip trace). Off until a key is set. ---
BATCHDATA_API_KEY = os.getenv("BATCHDATA_API_KEY", "")
BATCHDATA_URL = os.getenv("BATCHDATA_URL", "https://api.batchdata.com/api/v3/property/skip-trace")

# Optional: Claude drafts the outreach letter on the lead page.
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-5-5")

# Your brokerage's name and signature for outreach letters.
BROKERAGE_NAME = os.getenv("BROKERAGE_NAME", "Conquest")
AGENT_NAME = os.getenv("AGENT_NAME", "")
AGENT_PHONE = os.getenv("AGENT_PHONE", "")
AGENT_EMAIL = os.getenv("AGENT_EMAIL", "")
