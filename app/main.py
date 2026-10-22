"""SiteScope web app: audit box, SEO landing pages, report pages and JSON API."""
from __future__ import annotations

import asyncio
import logging
from urllib.parse import urlencode
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markupsafe import Markup
from pydantic import BaseModel

from . import config, mailer
from . import accounts as acc
from .audit import run_audit
from .compare import compare
from .crawler import normalise
from .pdf import build_pdf
from .ratelimit import client_ip, make_limiter
from .safety import BlockedTarget, check_url
from .store import make_store

log = logging.getLogger("sitescope")
store = make_store()
accounts = acc.make_accounts()
BASE = Path(__file__).parent


@asynccontextmanager
async def lifespan(_app):
    for _ in range(15):  # the database may still be starting in Docker
        try:
            store.init()
            break
        except Exception as exc:
            log.warning("store init failed (%s), retrying", exc)
            await asyncio.sleep(2)
    else:
        store.init()
    for part in (limiter, auth_limiter, accounts):
        try:
            part.init()
        except Exception as exc:
            log.warning("%s init failed (%s)", type(part).__name__, exc)
    # Audits run as background tasks inside this process, so any that were queued or
    # running when the process stopped will never finish. Mark them failed.
    stale = store.fail_unfinished()
    if stale:
        log.warning("marked %d unfinished audits as failed after restart", stale)
    task = asyncio.create_task(_scheduler()) if config.SCHEDULER_INTERVAL_S > 0 else None
    yield
    if task:
        task.cancel()


class HeadAsGet:
    """Answer HEAD like GET without a body. FastAPI routes are GET-only, so uptime
    monitors and crawlers that send HEAD would otherwise get 405 on every page."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] != "HEAD":
            return await self.app(scope, receive, send)

        async def send_without_body(message):
            if message["type"] == "http.response.body":
                message = {"type": "http.response.body", "body": b"",
                           "more_body": message.get("more_body", False)}
            await send(message)

        await self.app(dict(scope, method="GET"), receive, send_without_body)


app = FastAPI(title="SiteScope", docs_url="/api/docs", redoc_url=None, lifespan=lifespan)
app.add_middleware(HeadAsGet)
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
templates = Jinja2Templates(directory=BASE / "templates")
templates.env.globals["max_pages"] = config.MAX_PAGES
templates.env.globals["ga4_id"] = config.GA4_ID
# The stylesheet is small (~4 KB), so it is inlined in every page instead of being a
# render-blocking request; this cut mobile LCP in PageSpeed Insights.
templates.env.globals["inline_css"] = Markup((BASE / "static" / "style.css").read_text())
templates.env.globals["gsc_verification"] = config.GSC_VERIFICATION
MAX_RUNNING = 3
_running = asyncio.Semaphore(MAX_RUNNING)
limiter = make_limiter()  # Postgres when DATABASE_URL is set, so restarts don't reset it
auth_limiter = make_limiter(config.AUTH_ATTEMPTS_PER_IP_PER_HOUR)  # sign-ups and failed logins


def _limit_message(wait_s: int) -> str:
    return (f"Too many audits from your network. You can start {config.AUDITS_PER_IP_PER_HOUR} per hour; "
            f"try again in {max(1, round(wait_s / 60))} minutes.")

# slug -> (template, title, meta description)
PAGES = {
    "/": ("home.html", "Free SEO Audit Tool: Check Your Whole Website | SiteScope",
          "SiteScope is a free SEO audit tool that crawls your whole website and ranks technical SEO issues by priority. No sign-up needed."),
    "/audit/": ("audit.html", "Technical SEO Audit for Your Whole Site | SiteScope",
                "Run a free technical SEO audit: broken links, duplicate content, internal linking, titles, meta tags and Core Web Vitals in one report."),
    "/javascript-seo-checker/": ("javascript.html", "JavaScript SEO Checker: Can Google Read It? | SiteScope",
                                 "Check if your React or JavaScript site hides content and links from search engines and AI crawlers. Free JavaScript SEO checker."),
    "/core-web-vitals-checker/": ("cwv.html", "Core Web Vitals Checker: LCP, INP and CLS | SiteScope",
                                  "Check LCP, INP and CLS for your page with real Chrome user data and lab data, and learn what each Core Web Vital means."),
    "/broken-link-checker/": ("broken.html", "Free Broken Link Checker for Your Whole Website | SiteScope",
                              "Find broken internal and external links across your website. SiteScope crawls every page and lists each dead link and where it is."),
    "/how-issues-are-ranked/": ("ranking.html", "How SEO Issues Are Prioritised | SiteScope",
                                "SiteScope ranks every issue by severity and the share of pages it affects, so you know what to fix first."),
    "/screaming-frog-alternative/": ("frog.html", "Free Screaming Frog Alternative in Your Browser | SiteScope",
                                     "Looking for a free Screaming Frog alternative? Compare SiteScope with the free Screaming Frog SEO Spider, its 500 URL limit and desktop install."),
    "/is-my-react-website-seo-friendly/": ("guide_react.html", "Is My React Website SEO Friendly? | SiteScope",
                                           "Check in ten minutes whether Google and AI crawlers can read your React site: raw HTML, titles, links, status codes and the fixes that matter."),
    "/free-seo-audit-small-business/": ("guide_audit.html", "Free SEO Audit for a Small Business Website | SiteScope",
                                        "Audit your small business website for SEO for free: indexing, robots.txt, broken links, titles, duplicates, internal links and speed, in order."),
    "/about/": ("about.html", "About SiteScope: Free Technical SEO Audits",
                "SiteScope is a free technical SEO audit tool built as a student project at Bennett University for small startups and student websites."),
    "/privacy/": ("privacy.html", "Privacy Policy for the SiteScope SEO Audit Tool",
                  "What SiteScope stores when you run an audit, which analytics it uses, and how to ask for an audit to be deleted."),
}


def _render(request: Request, path: str, **ctx) -> HTMLResponse:
    template, title, desc = PAGES[path]
    return templates.TemplateResponse(request, template, {
        "title": title, "description": desc, "canonical": config.SITE_URL + path, "path": path, **ctx,
    })


def _make_page_route(path: str):
    async def page(request: Request):
        return _render(request, path, error=request.query_params.get("error"))
    return page


for _path in PAGES:
    app.add_api_route(_path, _make_page_route(_path), methods=["GET"], response_class=HTMLResponse,
                      include_in_schema=False)


def _validate(url: str) -> str:
    raw = url.strip()
    if not raw:
        raise ValueError("Please enter a website URL.")
    target = normalise(raw if "://" in raw else f"https://{raw}")
    if not target:
        raise ValueError("That does not look like a valid URL.")
    try:
        check_url(target)
    except BlockedTarget as exc:
        raise ValueError(str(exc)) from exc
    return target


async def _job(audit_id: str, url: str, max_pages: int | None) -> None:
    async with _running:
        store.update(audit_id, "running")
        try:
            result = await run_audit(url, max_pages=max_pages)
            store.update(audit_id, "done", result=result)
        except Exception as exc:
            log.exception("audit %s failed", audit_id)
            store.update(audit_id, "failed", error=f"{type(exc).__name__}: {exc}"[:500])


@app.post("/start", include_in_schema=False)
async def start_form(request: Request, background: BackgroundTasks, url: str = Form(...), back: str = Form("/")):
    back = back if back in PAGES else "/"
    try:
        target = _validate(url)
    except ValueError as exc:
        return RedirectResponse(f"{back}?{urlencode({'error': str(exc)})}", status_code=303)
    wait = limiter.retry_after(client_ip(request))
    if wait:
        return RedirectResponse(f"{back}?{urlencode({'error': _limit_message(wait)})}", status_code=303)
    audit_id = store.create(target)
    background.add_task(_job, audit_id, target, None)
    return RedirectResponse(f"/report/{audit_id}/", status_code=303)


@app.get("/report/{audit_id}/", response_class=HTMLResponse, include_in_schema=False)
async def report(request: Request, audit_id: str):
    row = store.get(audit_id)
    if not row:
        raise HTTPException(404, "Audit not found")
    prev, comparison = _comparison(row)
    return templates.TemplateResponse(request, "report.html", {
        "title": f"SEO audit for {row['url']} | SiteScope",
        "description": "SiteScope technical SEO audit report.",
        "canonical": f"{config.SITE_URL}/report/{audit_id}/",
        "path": "/report/", "row": row, "r": row.get("result"), "noindex": True,
        "prev": prev, "cmp": comparison,
    })


def _comparison(row: dict) -> tuple[dict | None, dict | None]:
    """The previous finished audit of the same URL, and what changed since then."""
    if row.get("status") != "done" or not row.get("result"):
        return None, None
    prev = store.previous(row["url"], row["created_at"], row["id"])
    if not prev or not prev.get("result"):
        return None, None
    return prev, compare(prev["result"], row["result"])


@app.get("/report/{audit_id}/pdf", include_in_schema=False)
async def report_pdf(audit_id: str):
    row = store.get(audit_id)
    if not row:
        raise HTTPException(404, "Audit not found")
    if row["status"] != "done" or not row.get("result"):
        raise HTTPException(409, "The audit has not finished yet")
    _, comparison = _comparison(row)
    pdf = await asyncio.to_thread(build_pdf, row, row["result"], comparison)
    host = (row["result"].get("url") or row["url"]).split("//")[-1].split("/")[0] or "site"
    return Response(pdf, media_type="application/pdf", headers={
        "Content-Disposition": f'attachment; filename="sitescope-{host}-{audit_id}.pdf"',
        "X-Robots-Tag": "noindex",
    })


class AuditIn(BaseModel):
    url: str
    max_pages: int | None = None


@app.post("/api/audits", status_code=202)
async def api_create(body: AuditIn, request: Request, background: BackgroundTasks):
    try:
        target = _validate(body.url)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    wait = limiter.retry_after(client_ip(request))
    if wait:
        raise HTTPException(429, _limit_message(wait), headers={"Retry-After": str(wait)})
    max_pages = min(body.max_pages, config.MAX_PAGES) if body.max_pages else None
    audit_id = store.create(target)
    background.add_task(_job, audit_id, target, max_pages)
    return {"id": audit_id, "status": "queued", "report_url": f"/report/{audit_id}/",
            "pdf_url": f"/report/{audit_id}/pdf"}


@app.get("/api/audits/{audit_id}")
async def api_get(audit_id: str):
    row = store.get(audit_id)
    if not row:
        raise HTTPException(404, "Audit not found")
    return JSONResponse({k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in row.items()})


@app.get("/health", include_in_schema=False)
async def health():
    return {"status": "ok"}


@app.get("/robots.txt", response_class=PlainTextResponse, include_in_schema=False)
async def robots():
    return (
        "User-agent: *\n"
        "Disallow: /report/\n"
        "Disallow: /api/\n"
        "Disallow: /account/\n"
        f"Sitemap: {config.SITE_URL}/sitemap.xml\n"
    )


@app.get("/sitemap.xml", include_in_schema=False)
async def sitemap():
    urls = "".join(f"<url><loc>{config.SITE_URL}{p}</loc></url>" for p in PAGES)
    xml = f'<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{urls}</urlset>'
    return Response(xml, media_type="application/xml")


# --- accounts -------------------------------------------------------------------
# Audits stay free without an account. An account only adds monitored sites: each one
# is re-audited daily or weekly, and the owner gets an email when the health score drops.

SESSION_COOKIE = "ss_session"
# Each scrypt hash uses ~16 MB; cap parallel hashing so a burst of log-ins can't
# exhaust the 1 GiB VM.
_hash_slots = asyncio.Semaphore(2)


async def _scrypt(fn, *args):
    async with _hash_slots:
        return await asyncio.to_thread(fn, *args)


def current_user(request: Request) -> dict | None:
    token = request.cookies.get(SESSION_COOKIE)
    return accounts.session_user(acc.token_hash(token)) if token else None


def _same_origin(request: Request) -> bool:
    """Reject cross-site form posts. The SameSite=Lax cookie already blocks most;
    this also covers browsers that send an Origin header but not SameSite."""
    origin = request.headers.get("origin")
    if not origin or origin == "null":
        return origin is None
    return origin.split("://", 1)[-1].rstrip("/") == request.headers.get("host", "")


def _back(path: str, **params) -> RedirectResponse:
    q = urlencode({k: v for k, v in params.items() if v})
    return RedirectResponse(f"{path}?{q}" if q else path, status_code=303)


def _start_session(user_id: str, to: str, **params) -> RedirectResponse:
    token, th = acc.new_token()
    accounts.create_session(user_id, th)
    res = _back(to, **params)
    res.set_cookie(SESSION_COOKIE, token, max_age=acc.SESSION_DAYS * 86400, httponly=True,
                   secure=config.COOKIE_SECURE, samesite="lax", path="/")
    return res


def _account_page(request: Request, template: str, title: str, **ctx) -> HTMLResponse:
    return templates.TemplateResponse(request, template, {
        "title": f"{title} | SiteScope", "description": "Your SiteScope account.",
        "canonical": f"{config.SITE_URL}/account/", "path": "/account/", "noindex": True,
        "error": request.query_params.get("error"), "msg": request.query_params.get("msg"), **ctx,
    })


async def _send_verification(email: str, token: str) -> bool:
    link = f"{config.SITE_URL}/account/verify/?{urlencode({'token': token})}"
    return await mailer.send(email, "Confirm your SiteScope email",
                             f"Open this link to confirm your email and turn on score alerts:\n\n{link}\n\n"
                             "If you did not sign up for SiteScope, ignore this email.")


@app.get("/account/login/", response_class=HTMLResponse, include_in_schema=False)
async def account_login_page(request: Request):
    if current_user(request):
        return _back("/account/")
    return _account_page(request, "account_login.html", "Log in or sign up")


@app.post("/account/signup", include_in_schema=False)
async def account_signup(request: Request, email: str = Form(...), password: str = Form(...)):
    if not _same_origin(request):
        raise HTTPException(403, "Cross-site request refused")
    email = acc.normalise_email(email)
    if not acc.valid_email(email):
        return _back("/account/login/", error="Please enter a valid email address.")
    if not 8 <= len(password) <= 200:
        return _back("/account/login/", error="Use a password of at least 8 characters.")
    if auth_limiter.retry_after(client_ip(request)):
        return _back("/account/login/", error="Too many attempts from your network. Try again later.")
    token, th = acc.new_token()
    pw_hash = await _scrypt(acc.hash_password, password)
    user_id = accounts.create_user(email, pw_hash, th)
    if not user_id:
        return _back("/account/login/", error="That email already has an account. Log in instead.")
    sent = await _send_verification(email, token)
    msg = ("Account created. Check your email for a link to turn on score alerts." if sent
           else "Account created. Email alerts are switched off on this server for now.")
    return _start_session(user_id, "/account/", msg=msg)


@app.post("/account/login", include_in_schema=False)
async def account_login(request: Request, email: str = Form(...), password: str = Form(...)):
    if not _same_origin(request):
        raise HTTPException(403, "Cross-site request refused")
    # Every attempt counts, so a blocked IP can't keep guessing until one succeeds.
    if auth_limiter.retry_after(client_ip(request)):
        return _back("/account/login/", error="Too many attempts from your network. Try again later.")
    user = accounts.user_by_email(acc.normalise_email(email))
    ok = await _scrypt(acc.check_password, password[:200],
                                 user["pw_hash"] if user else acc._DUMMY_HASH)
    if not (user and ok):
        return _back("/account/login/", error="Wrong email or password.")
    return _start_session(user["id"], "/account/")


@app.post("/account/logout", include_in_schema=False)
async def account_logout(request: Request):
    if not _same_origin(request):
        raise HTTPException(403, "Cross-site request refused")
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        accounts.delete_session(acc.token_hash(token))
    res = _back("/")
    res.delete_cookie(SESSION_COOKIE, path="/")
    return res


@app.get("/account/verify/", include_in_schema=False)
async def account_verify(token: str = ""):
    ok = bool(token) and accounts.verify(acc.token_hash(token))
    return _back("/account/", msg="Email confirmed. Score alerts are on." if ok else None,
                 error=None if ok else "That confirmation link is not valid any more.")


@app.get("/account/", response_class=HTMLResponse, include_in_schema=False)
async def account_home(request: Request):
    user = current_user(request)
    if not user:
        return _back("/account/login/")
    return _account_page(request, "account.html", "Your monitored sites", user=user,
                         monitors=accounts.monitors_for(user["id"]), mail_on=mailer.enabled(),
                         max_monitors=config.MAX_MONITORS_PER_USER, drop=config.ALERT_DROP_POINTS)


@app.post("/account/sites", include_in_schema=False)
async def account_add_site(request: Request, url: str = Form(...), frequency: str = Form("weekly")):
    user = current_user(request)
    if not user or not _same_origin(request):
        return _back("/account/login/")
    if frequency not in acc.FREQUENCIES:
        frequency = "weekly"
    if len(accounts.monitors_for(user["id"])) >= config.MAX_MONITORS_PER_USER:
        return _back("/account/", error=f"You can monitor up to {config.MAX_MONITORS_PER_USER} sites.")
    try:
        target = _validate(url)
    except ValueError as exc:
        return _back("/account/", error=str(exc))
    if not accounts.add_monitor(user["id"], target, frequency):
        return _back("/account/", error="You already monitor that site.")
    return _back("/account/", msg="Site added. The first audit starts within a minute.")


@app.post("/account/sites/{mid}/run", include_in_schema=False)
async def account_run_site(request: Request, mid: str):
    user = current_user(request)
    if not user or not _same_origin(request):
        return _back("/account/login/")
    wait = limiter.retry_after(client_ip(request))  # counts like any other audit
    if wait:
        return _back("/account/", error=_limit_message(wait))
    accounts.run_now(user["id"], mid)
    return _back("/account/", msg="Queued. The audit starts within a minute.")


@app.post("/account/sites/{mid}/delete", include_in_schema=False)
async def account_delete_site(request: Request, mid: str):
    user = current_user(request)
    if not user or not _same_origin(request):
        return _back("/account/login/")
    accounts.delete_monitor(user["id"], mid)
    return _back("/account/", msg="Site removed.")


@app.post("/account/delete", include_in_schema=False)
async def account_delete(request: Request, password: str = Form(...)):
    user = current_user(request)
    if not user or not _same_origin(request):
        return _back("/account/login/")
    if not await _scrypt(acc.check_password, password[:200], user["pw_hash"]):
        return _back("/account/", error="Wrong password. The account was not deleted.")
    accounts.delete_user(user["id"])
    res = _back("/")
    res.delete_cookie(SESSION_COOKIE, path="/")
    return res


# --- scheduled re-audits --------------------------------------------------------

async def _run_monitor(m: dict) -> None:
    try:
        target = _validate(m["url"])  # re-check: DNS may now point somewhere private
    except ValueError as exc:
        log.warning("monitor %s skipped: %s", m["id"], exc)
        return
    audit_id = store.create(target)
    await _job(audit_id, target, None)
    row = store.get(audit_id) or {}
    result = row.get("result") if row.get("status") == "done" else None
    score = result.get("health_score") if result else None
    accounts.set_result(m["id"], audit_id, score)
    prev = m.get("last_score")
    if score is None or prev is None or prev - score < config.ALERT_DROP_POINTS or not m.get("verified"):
        return
    await mailer.send(
        m["email"], f"SiteScope: health score for {target} fell from {prev} to {score}",
        f"The scheduled audit of {target} finished with a health score of {score}, down from {prev}.\n\n"
        f"Report (with what is new since the last audit): {config.SITE_URL}/report/{audit_id}/\n\n"
        f"Manage your monitored sites: {config.SITE_URL}/account/")


async def run_due_monitors() -> int:
    """Run every monitored site that is due. Returns how many were started."""
    due = accounts.claim_due(limit=MAX_RUNNING)
    for m in due:
        await _run_monitor(m)
    return len(due)


async def _scheduler() -> None:
    while True:
        try:
            await run_due_monitors()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("scheduler pass failed")
        await asyncio.sleep(config.SCHEDULER_INTERVAL_S)
