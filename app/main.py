"""SiteScope web app: audit box, SEO landing pages, report pages and JSON API."""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markupsafe import Markup
from pydantic import BaseModel

from . import config
from .audit import run_audit
from .crawler import normalise
from .safety import BlockedTarget, check_url
from .store import make_store

log = logging.getLogger("sitescope")
store = make_store()
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
    yield


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
async def start_form(background: BackgroundTasks, url: str = Form(...), back: str = Form("/")):
    back = back if back in PAGES else "/"
    try:
        target = _validate(url)
    except ValueError as exc:
        return RedirectResponse(f"{back}?error={exc}", status_code=303)
    audit_id = store.create(target)
    background.add_task(_job, audit_id, target, None)
    return RedirectResponse(f"/report/{audit_id}/", status_code=303)


@app.get("/report/{audit_id}/", response_class=HTMLResponse, include_in_schema=False)
async def report(request: Request, audit_id: str):
    row = store.get(audit_id)
    if not row:
        raise HTTPException(404, "Audit not found")
    return templates.TemplateResponse(request, "report.html", {
        "title": f"SEO audit for {row['url']} | SiteScope",
        "description": "SiteScope technical SEO audit report.",
        "canonical": f"{config.SITE_URL}/report/{audit_id}/",
        "path": "/report/", "row": row, "r": row.get("result"), "noindex": True,
    })


class AuditIn(BaseModel):
    url: str
    max_pages: int | None = None


@app.post("/api/audits", status_code=202)
async def api_create(body: AuditIn, background: BackgroundTasks):
    try:
        target = _validate(body.url)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    max_pages = min(body.max_pages, config.MAX_PAGES) if body.max_pages else None
    audit_id = store.create(target)
    background.add_task(_job, audit_id, target, max_pages)
    return {"id": audit_id, "status": "queued", "report_url": f"/report/{audit_id}/"}


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
        f"Sitemap: {config.SITE_URL}/sitemap.xml\n"
    )


@app.get("/sitemap.xml", include_in_schema=False)
async def sitemap():
    urls = "".join(f"<url><loc>{config.SITE_URL}{p}</loc></url>" for p in PAGES)
    xml = f'<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{urls}</urlset>'
    return Response(xml, media_type="application/xml")
