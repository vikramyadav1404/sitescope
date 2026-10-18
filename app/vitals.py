"""Core Web Vitals from the PageSpeed Insights API (lab + CrUX field data),
and an optional JavaScript rendering diff using Playwright."""
from __future__ import annotations

import httpx

from . import config

PSI = "https://www.googleapis.com/pagespeedonline/v5/runPagespeed"


async def core_web_vitals(url: str, client: httpx.AsyncClient | None = None) -> dict:
    params = {"url": url, "strategy": "mobile", "category": "performance"}
    if config.PAGESPEED_API_KEY:
        params["key"] = config.PAGESPEED_API_KEY
    own = client is None
    client = client or httpx.AsyncClient(timeout=60)
    try:
        r = await client.get(PSI, params=params)
        if r.status_code != 200:
            return {"available": False, "reason": f"PageSpeed Insights returned HTTP {r.status_code}"}
        data = r.json()
    except (httpx.HTTPError, ValueError) as exc:
        return {"available": False, "reason": type(exc).__name__}
    finally:
        if own:
            await client.aclose()

    audits = data.get("lighthouseResult", {}).get("audits", {})
    field = data.get("loadingExperience", {}).get("metrics", {})
    score = data.get("lighthouseResult", {}).get("categories", {}).get("performance", {}).get("score")

    def lab(key: str):
        return audits.get(key, {}).get("numericValue")

    def fld(key: str):
        return field.get(key, {}).get("percentile")

    lcp_ms = fld("LARGEST_CONTENTFUL_PAINT_MS") or lab("largest-contentful-paint")
    cls = fld("CUMULATIVE_LAYOUT_SHIFT_SCORE")
    cls = cls / 100 if cls is not None else lab("cumulative-layout-shift")
    inp_ms = fld("INTERACTION_TO_NEXT_PAINT")
    return {
        "available": True,
        "source": "field (CrUX)" if field else "lab (Lighthouse)",
        "performance_score": round(score * 100) if score is not None else None,
        "lcp_ms": round(lcp_ms) if lcp_ms is not None else None,
        "cls": round(cls, 3) if cls is not None else None,
        "inp_ms": round(inp_ms) if inp_ms is not None else None,
    }


async def js_render_diff(url: str, raw_word_count: int) -> dict:
    """Compare raw-HTML word count with the Playwright-rendered page.
    Runs only when Playwright is installed (RENDER_JS is optional)."""
    try:
        from playwright.async_api import async_playwright  # type: ignore
    except ImportError:
        return {"available": False, "reason": "Playwright not installed"}
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            page = await browser.new_page(user_agent=config.USER_AGENT)
            await page.goto(url, wait_until="networkidle", timeout=30000)
            text = await page.evaluate("document.body ? document.body.innerText : ''")
            await browser.close()
    except Exception as exc:  # browser errors vary by platform
        return {"available": False, "reason": type(exc).__name__}
    rendered = len(text.split())
    share = 0 if rendered == 0 else round(100 * max(rendered - raw_word_count, 0) / rendered, 1)
    return {"available": True, "raw_words": raw_word_count, "rendered_words": rendered, "js_only_pct": share}
