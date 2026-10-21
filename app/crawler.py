"""Async breadth-first crawler with robots.txt, sitemap discovery,
rate limiting and URL normalisation."""
from __future__ import annotations

import asyncio
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

import httpx
from bs4 import BeautifulSoup

from . import config
from .safety import BlockedTarget, guard_request, guarded_transport

SKIP_EXT = (
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".ico", ".pdf", ".zip",
    ".mp4", ".mp3", ".css", ".js", ".woff", ".woff2", ".ttf", ".xml", ".json",
)


def normalise(url: str, base: str | None = None) -> str | None:
    """Absolute URL, lower-case host, no fragment, no default port."""
    if base:
        url = urljoin(base, url)
    parts = urlsplit(url.strip())
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return None
    host = parts.hostname.lower() if parts.hostname else ""
    port = parts.port
    if port and not ((parts.scheme == "http" and port == 80) or (parts.scheme == "https" and port == 443)):
        host = f"{host}:{port}"
    path = parts.path or "/"
    return urlunsplit((parts.scheme, host, path, parts.query, ""))


def same_site(url: str, root_host: str) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    return host == root_host or host == f"www.{root_host}" or f"www.{host}" == root_host


@dataclass
class Page:
    url: str
    depth: int
    status: int = 0
    final_url: str = ""
    redirects: list[str] = field(default_factory=list)
    content_type: str = ""
    x_robots: str = ""
    elapsed_ms: int = 0
    html: str = ""
    internal_links: list[str] = field(default_factory=list)
    external_links: list[str] = field(default_factory=list)
    error: str = ""


@dataclass
class CrawlResult:
    start_url: str
    pages: dict[str, Page]
    sitemap_urls: set[str]
    robots_blocked: list[str]
    link_status: dict[str, int]  # linked URL -> HTTP status (0 = network error)


class Crawler:
    def __init__(self, start_url: str, max_pages: int | None = None,
                 max_depth: int | None = None, client: httpx.AsyncClient | None = None):
        start = normalise(start_url if "://" in start_url else f"https://{start_url}")
        if not start:
            raise ValueError("Invalid URL")
        self.start_url = start
        self.root_host = (urlsplit(start).hostname or "").lower().removeprefix("www.")
        self.max_pages = max_pages or config.MAX_PAGES
        self.max_depth = max_depth if max_depth is not None else config.MAX_DEPTH
        self._client = client
        self._robots = RobotFileParser()
        self._robots_loaded = False
        self._sem = asyncio.Semaphore(config.CONCURRENCY)
        self._delay = config.REQUEST_DELAY_MS / 1000

    async def _get(self, client: httpx.AsyncClient, url: str) -> httpx.Response:
        async with self._sem:
            if self._delay:
                await asyncio.sleep(self._delay)
            return await client.get(url)

    async def _load_robots(self, client: httpx.AsyncClient) -> list[str]:
        """Parse robots.txt; return any Sitemap: URLs it lists."""
        robots_url = urljoin(self.start_url, "/robots.txt")
        try:
            r = await self._get(client, robots_url)
            if r.status_code == 200:
                lines = r.text.splitlines()
                self._robots.parse(lines)
                self._robots_loaded = True
                return [l.split(":", 1)[1].strip() for l in lines if l.lower().startswith("sitemap:")]
        except (httpx.HTTPError, BlockedTarget):
            pass
        self._robots.parse([])  # nothing disallowed
        self._robots_loaded = True
        return []

    def allowed(self, url: str) -> bool:
        return self._robots.can_fetch(config.USER_AGENT, url)

    async def _load_sitemaps(self, client: httpx.AsyncClient, urls: list[str]) -> set[str]:
        found: set[str] = set()
        queue = urls or [urljoin(self.start_url, "/sitemap.xml")]
        seen: set[str] = set()
        while queue and len(seen) < 10:
            sm = queue.pop(0)
            if sm in seen:
                continue
            seen.add(sm)
            try:
                r = await self._get(client, sm)
                if r.status_code != 200:
                    continue
                root = ET.fromstring(r.content)
            except (httpx.HTTPError, ET.ParseError, BlockedTarget):
                continue
            for loc in root.iter():
                if not loc.tag.endswith("loc") or not loc.text:
                    continue
                u = normalise(loc.text)
                if not u:
                    continue
                if root.tag.endswith("sitemapindex"):
                    queue.append(u)
                elif same_site(u, self.root_host):
                    found.add(u)
        return found

    async def _fetch_page(self, client: httpx.AsyncClient, page: Page) -> None:
        t0 = time.perf_counter()
        try:
            r = await self._get(client, page.url)
        except (httpx.HTTPError, BlockedTarget) as exc:
            page.error = type(exc).__name__
            return
        page.elapsed_ms = int((time.perf_counter() - t0) * 1000)
        page.status = r.status_code
        page.final_url = str(r.url)
        page.redirects = [str(h.url) for h in r.history]
        page.content_type = r.headers.get("content-type", "")
        page.x_robots = r.headers.get("x-robots-tag", "")
        if "text/html" in page.content_type and r.status_code < 400:
            page.html = r.text
            soup = BeautifulSoup(page.html, "html.parser")
            base = page.final_url or page.url
            for a in soup.find_all("a", href=True):
                href = a["href"].strip()
                if href.startswith(("mailto:", "tel:", "javascript:", "#")):
                    continue
                u = normalise(href, base)
                if not u:
                    continue
                if same_site(u, self.root_host):
                    page.internal_links.append(u)
                else:
                    page.external_links.append(u)

    async def _check_links(self, client: httpx.AsyncClient, urls: set[str]) -> dict[str, int]:
        out: dict[str, int] = {}

        async def check(u: str) -> None:
            try:
                async with self._sem:
                    r = await client.head(u)
                    if r.status_code in (403, 405, 501):  # some servers reject HEAD
                        r = await client.get(u)
                out[u] = r.status_code
            except (httpx.HTTPError, BlockedTarget):
                out[u] = 0

        await asyncio.gather(*(check(u) for u in list(urls)[:200]))
        return out

    async def run(self) -> CrawlResult:
        own = self._client is None
        client = self._client or httpx.AsyncClient(
            headers={"User-Agent": config.USER_AGENT},
            timeout=config.TIMEOUT_S, follow_redirects=True,
            event_hooks={"request": [guard_request]},
            transport=guarded_transport(), trust_env=False,  # connect only to the IP the guard checked
        )
        try:
            sitemap_refs = await self._load_robots(client)
            sitemap_urls = await self._load_sitemaps(client, sitemap_refs)

            pages: dict[str, Page] = {}
            blocked: list[str] = []
            seen = {self.start_url}
            frontier = [self.start_url]
            depth = 0
            while frontier and len(pages) < self.max_pages and depth <= self.max_depth:
                batch = []
                for u in frontier:
                    if len(pages) + len(batch) >= self.max_pages:
                        break
                    if not self.allowed(u):
                        blocked.append(u)
                        continue
                    batch.append(Page(url=u, depth=depth))
                await asyncio.gather(*(self._fetch_page(client, p) for p in batch))
                next_frontier = []
                for p in batch:
                    pages[p.url] = p
                    for link in p.internal_links:
                        if link not in seen and not link.lower().endswith(SKIP_EXT):
                            seen.add(link)
                            next_frontier.append(link)
                frontier = next_frontier
                depth += 1

            # status of every linked URL we did not crawl ourselves
            linked = {l for p in pages.values() for l in p.internal_links + p.external_links}
            to_check = {u for u in linked if u not in pages}
            link_status = {u: p.status for u, p in pages.items()}
            link_status.update(await self._check_links(client, to_check))
            return CrawlResult(self.start_url, pages, sitemap_urls, blocked, link_status)
        finally:
            if own:
                await client.aclose()
