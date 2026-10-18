"""Settings read from environment variables (see .env.example)."""
import os


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except ValueError:
        return default


DATABASE_URL = os.getenv("DATABASE_URL", "")  # empty = in-memory store (dev/tests)
PAGESPEED_API_KEY = os.getenv("PAGESPEED_API_KEY", "")
SITE_URL = os.getenv("SITE_URL", "https://vikramyadav.me").rstrip("/")

MAX_PAGES = _int("MAX_PAGES", 300)          # pages crawled per audit
MAX_DEPTH = _int("MAX_DEPTH", 6)            # click depth limit
CONCURRENCY = _int("CONCURRENCY", 5)        # parallel requests
REQUEST_DELAY_MS = _int("REQUEST_DELAY_MS", 200)  # politeness delay per request
TIMEOUT_S = _int("TIMEOUT_S", 15)
USER_AGENT = os.getenv(
    "USER_AGENT", "SiteScopeBot/1.0 (+https://vikramyadav.me/about/)"
)
