"""On-page and site-level checks. Each issue has a severity:
3 = Critical, 2 = Warning, 1 = Notice.
Priority = Severity x % of pages affected (0-300)."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from bs4 import BeautifulSoup

from .crawler import CrawlResult, Page

LEVEL = {3: "Critical", 2: "Warning", 1: "Notice"}

# id: (severity, title, how to fix)
ISSUE_TYPES: dict[str, tuple[int, str, str]] = {
    "server_error": (3, "Page returns a 5xx server error", "Check server logs and fix the failing route."),
    "client_error": (3, "Page returns a 4xx error", "Fix or remove internal links that point here, or restore the page."),
    "noindex": (3, "Page is set to noindex", "Remove the noindex meta tag or X-Robots-Tag header if the page should rank."),
    "broken_internal_link": (2, "Links to a broken internal URL", "Update or remove links that return 4xx/5xx."),
    "broken_external_link": (1, "Links to a broken external URL", "Replace or remove dead outbound links."),
    "missing_title": (2, "Missing <title> tag", "Add a unique title of 30-60 characters."),
    "title_length": (1, "Title shorter than 30 or longer than 60 characters", "Rewrite the title to 30-60 characters."),
    "duplicate_title": (2, "Title used on more than one page", "Give every page a unique title."),
    "missing_meta_description": (2, "Missing meta description", "Add a meta description of 70-160 characters."),
    "duplicate_meta_description": (1, "Meta description used on more than one page", "Write a unique description per page."),
    "missing_h1": (2, "Missing H1 heading", "Add one H1 that states the page topic."),
    "multiple_h1": (1, "More than one H1 heading", "Keep a single H1 and use H2/H3 below it."),
    "missing_canonical": (1, "Missing canonical tag", "Add <link rel=\"canonical\"> pointing to the preferred URL."),
    "missing_alt": (1, "Images without alt text", "Add short, descriptive alt text to each image."),
    "missing_lang": (1, "Missing lang attribute on <html>", "Add lang=\"en\" (or the page language)."),
    "thin_content": (1, "Fewer than 200 words of text", "Expand the page with useful content or merge it."),
    "redirect_chain": (2, "Reached through more than one redirect", "Link directly to the final URL."),
    "slow_response": (2, "Server response slower than 1.5 s", "Add caching or optimise the backend."),
    "deep_page": (1, "More than 3 clicks from the homepage", "Link to the page from the homepage or a hub page."),
    "orphan_page": (2, "In the sitemap but not linked from any crawled page", "Add internal links to the page."),
    "near_duplicate": (2, "Duplicate or near-duplicate content (SimHash)", "Merge the pages or add a canonical tag."),
    "slow_lcp": (2, "Largest Contentful Paint above 2.5 s", "Optimise the hero image, fonts and server response."),
    "poor_cls": (2, "Cumulative Layout Shift above 0.1", "Reserve space for images, ads and embeds."),
    "slow_inp": (2, "Interaction to Next Paint above 200 ms", "Reduce long JavaScript tasks."),
    "js_hidden_content": (2, "Main content appears only after JavaScript runs", "Server-render key content and links."),
}


@dataclass
class PageInfo:
    url: str
    status: int
    depth: int
    title: str = ""
    meta_description: str = ""
    h1: list[str] = field(default_factory=list)
    canonical: str = ""
    word_count: int = 0
    text: str = ""
    images_missing_alt: int = 0
    noindex: bool = False
    lang: str = ""
    elapsed_ms: int = 0


def parse_page(page: Page) -> PageInfo:
    info = PageInfo(url=page.url, status=page.status, depth=page.depth, elapsed_ms=page.elapsed_ms)
    if "noindex" in page.x_robots.lower():
        info.noindex = True
    if not page.html:
        return info
    soup = BeautifulSoup(page.html, "html.parser")
    html_tag = soup.find("html")
    info.lang = (html_tag.get("lang") or "").strip() if html_tag else ""
    if soup.title and soup.title.string:
        info.title = soup.title.string.strip()
    md = soup.find("meta", attrs={"name": lambda v: v and v.lower() == "description"})
    info.meta_description = (md.get("content") or "").strip() if md else ""
    robots = soup.find("meta", attrs={"name": lambda v: v and v.lower() in ("robots", "googlebot")})
    if robots and "noindex" in (robots.get("content") or "").lower():
        info.noindex = True
    can = soup.find("link", rel=lambda v: v and "canonical" in (v if isinstance(v, list) else [v]))
    info.canonical = (can.get("href") or "").strip() if can else ""
    info.h1 = [h.get_text(" ", strip=True) for h in soup.find_all("h1")]
    info.images_missing_alt = sum(1 for img in soup.find_all("img") if not (img.get("alt") or "").strip())
    for tag in soup(["script", "style", "noscript", "template"]):
        tag.decompose()
    info.text = soup.get_text(" ", strip=True)
    info.word_count = len(info.text.split())
    return info


def page_issues(page: Page, info: PageInfo, link_status: dict[str, int]) -> list[tuple[str, str]]:
    """Return (issue_id, detail) pairs for one page."""
    out: list[tuple[str, str]] = []
    if page.status >= 500:
        out.append(("server_error", f"HTTP {page.status}"))
    elif page.status >= 400:
        out.append(("client_error", f"HTTP {page.status}"))
    if page.error:
        out.append(("server_error", page.error))
    if len(page.redirects) > 1:
        out.append(("redirect_chain", f"{len(page.redirects)} redirects"))
    if page.elapsed_ms > 1500:
        out.append(("slow_response", f"{page.elapsed_ms} ms"))
    if info.noindex:
        out.append(("noindex", ""))
    if not page.html:
        return out
    if not info.title:
        out.append(("missing_title", ""))
    elif not 30 <= len(info.title) <= 60:
        out.append(("title_length", f"{len(info.title)} characters"))
    if not info.meta_description:
        out.append(("missing_meta_description", ""))
    if not info.h1:
        out.append(("missing_h1", ""))
    elif len(info.h1) > 1:
        out.append(("multiple_h1", f"{len(info.h1)} H1 tags"))
    if not info.canonical:
        out.append(("missing_canonical", ""))
    if info.images_missing_alt:
        out.append(("missing_alt", f"{info.images_missing_alt} images"))
    if not info.lang:
        out.append(("missing_lang", ""))
    if info.word_count < 200:
        out.append(("thin_content", f"{info.word_count} words"))
    if info.depth > 3:
        out.append(("deep_page", f"depth {info.depth}"))
    broken_int = sorted({l for l in page.internal_links if _broken(link_status.get(l))})
    broken_ext = sorted({l for l in page.external_links if _broken(link_status.get(l))})
    if broken_int:
        out.append(("broken_internal_link", ", ".join(broken_int[:5])))
    if broken_ext:
        out.append(("broken_external_link", ", ".join(broken_ext[:5])))
    return out


def _broken(status: int | None) -> bool:
    return status is not None and (status == 0 or status >= 400)


def duplicate_field_issues(infos: list[PageInfo]) -> list[tuple[str, str, str]]:
    """(issue_id, url, detail) for duplicate titles / meta descriptions."""
    out = []
    for attr, issue in (("title", "duplicate_title"), ("meta_description", "duplicate_meta_description")):
        groups: dict[str, list[str]] = defaultdict(list)
        for i in infos:
            val = getattr(i, attr)
            if val:
                groups[val].append(i.url)
        for val, urls in groups.items():
            if len(urls) > 1:
                for u in urls:
                    out.append((issue, u, f"shared by {len(urls)} pages"))
    return out


def prioritise(issue_pages: dict[str, dict[str, str]], total_pages: int) -> list[dict]:
    """Rank issues by Priority = Severity x % of pages affected."""
    ranked = []
    for issue_id, pages in issue_pages.items():
        sev, title, fix = ISSUE_TYPES[issue_id]
        pct = 100 * len(pages) / max(total_pages, 1)
        ranked.append({
            "id": issue_id,
            "title": title,
            "fix": fix,
            "severity": sev,
            "level": LEVEL[sev],
            "pages_affected": len(pages),
            "pct_pages": round(pct, 1),
            "priority": round(sev * pct, 1),
            "pages": [{"url": u, "detail": d} for u, d in sorted(pages.items())][:50],
        })
    ranked.sort(key=lambda r: (-r["priority"], -r["severity"], r["id"]))
    return ranked


def health_score(ranked: list[dict]) -> int:
    """0-100 score: starts at 100, loses points by priority (capped per issue)."""
    penalty = sum(min(r["priority"], 100) for r in ranked) / 10
    return max(0, round(100 - penalty))


def collect(result: CrawlResult) -> tuple[list[PageInfo], dict[str, dict[str, str]]]:
    infos = []
    issue_pages: dict[str, dict[str, str]] = defaultdict(dict)
    for page in result.pages.values():
        info = parse_page(page)
        infos.append(info)
        for issue_id, detail in page_issues(page, info, result.link_status):
            issue_pages[issue_id][page.url] = detail
    for issue_id, url, detail in duplicate_field_issues(infos):
        issue_pages[issue_id][url] = detail
    return infos, issue_pages
