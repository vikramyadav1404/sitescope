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
GA4_ID = os.getenv("GA4_ID", "").strip()  # e.g. G-XXXXXXXXXX; empty = no analytics tag
GSC_VERIFICATION = os.getenv("GSC_VERIFICATION", "").strip()  # Search Console HTML-tag token (optional)

MAX_PAGES = _int("MAX_PAGES", 300)          # pages crawled per audit
MAX_DEPTH = _int("MAX_DEPTH", 6)            # click depth limit
CONCURRENCY = _int("CONCURRENCY", 5)        # parallel requests
REQUEST_DELAY_MS = _int("REQUEST_DELAY_MS", 200)  # politeness delay per request
TIMEOUT_S = _int("TIMEOUT_S", 15)
AUDITS_PER_IP_PER_HOUR = _int("AUDITS_PER_IP_PER_HOUR", 10)  # 0 = no limit
USER_AGENT = os.getenv(
    "USER_AGENT", "SiteScopeBot/1.0 (+https://vikramyadav.me/about/)"
)

# Accounts and scheduled re-audits
COOKIE_SECURE = SITE_URL.startswith("https://")  # session cookie only over HTTPS in production
MAX_MONITORS_PER_USER = _int("MAX_MONITORS_PER_USER", 5)
ALERT_DROP_POINTS = _int("ALERT_DROP_POINTS", 5)     # email when the health score falls this much
SCHEDULER_INTERVAL_S = _int("SCHEDULER_INTERVAL_S", 60)  # how often to look for due re-audits; 0 = off
AUTH_ATTEMPTS_PER_IP_PER_HOUR = _int("AUTH_ATTEMPTS_PER_IP_PER_HOUR", 20)

# Outgoing email (verification links and score-drop alerts). Empty SMTP_HOST = no email.
SMTP_HOST = os.getenv("SMTP_HOST", "").strip()
SMTP_PORT = _int("SMTP_PORT", 587)
SMTP_USER = os.getenv("SMTP_USER", "").strip()
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
MAIL_FROM = os.getenv("MAIL_FROM", "").strip()  # e.g. SiteScope <alerts@vikramyadav.me>
