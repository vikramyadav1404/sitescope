"""Runs a full audit: crawl -> checks -> link graph -> duplicates -> vitals."""
from __future__ import annotations

import os
from datetime import datetime, timezone

from . import checks, linkgraph, simhash, vitals
from .crawler import Crawler


async def run_audit(url: str, max_pages: int | None = None, with_vitals: bool = True,
                    client=None, psi_client=None) -> dict:
    started = datetime.now(timezone.utc)
    crawler = Crawler(url, max_pages=max_pages, client=client)
    result = await crawler.run()

    infos, issue_pages = checks.collect(result)
    graph = linkgraph.analyse(result)
    for u in graph["orphans"]:
        issue_pages["orphan_page"][u] = "listed in sitemap"

    hashes = {i.url: simhash.simhash(i.text) for i in infos if i.word_count >= 50}
    dupes = simhash.find_duplicates(hashes)
    for a, b, d in dupes:
        issue_pages["near_duplicate"][a] = f"similar to {b} (distance {d})"
        issue_pages["near_duplicate"][b] = f"similar to {a} (distance {d})"

    cwv = {"available": False, "reason": "skipped"}
    if with_vitals:
        cwv = await vitals.core_web_vitals(result.start_url, client=psi_client)
        if cwv.get("available"):
            if (cwv.get("lcp_ms") or 0) > 2500:
                issue_pages["slow_lcp"][result.start_url] = f"{cwv['lcp_ms']} ms"
            if (cwv.get("cls") or 0) > 0.1:
                issue_pages["poor_cls"][result.start_url] = str(cwv["cls"])
            if (cwv.get("inp_ms") or 0) > 200:
                issue_pages["slow_inp"][result.start_url] = f"{cwv['inp_ms']} ms"

    js = {"available": False, "reason": "disabled"}
    if os.getenv("RENDER_JS") == "1":
        home = next((i for i in infos if i.url == result.start_url), None)
        js = await vitals.js_render_diff(result.start_url, home.word_count if home else 0)
        if js.get("available") and js["js_only_pct"] >= 30:
            issue_pages["js_hidden_content"][result.start_url] = f"{js['js_only_pct']}% of words need JavaScript"

    issue_pages = {k: v for k, v in issue_pages.items() if v}
    html_pages = sum(1 for p in result.pages.values() if p.html)
    ranked = checks.prioritise(issue_pages, max(len(result.pages), 1))
    status_counts: dict[str, int] = {}
    for p in result.pages.values():
        key = str(p.status) if p.status else "error"
        status_counts[key] = status_counts.get(key, 0) + 1

    return {
        "url": result.start_url,
        "started_at": started.isoformat(timespec="seconds"),
        "finished_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "pages_crawled": len(result.pages),
        "html_pages": html_pages,
        "sitemap_urls": len(result.sitemap_urls),
        "robots_blocked": result.robots_blocked[:50],
        "status_counts": status_counts,
        "health_score": checks.health_score(ranked),
        "issues": ranked,
        "counts": {
            lvl: sum(1 for r in ranked if r["level"] == lvl) for lvl in ("Critical", "Warning", "Notice")
        },
        "link_graph": graph,
        "duplicates": [{"a": a, "b": b, "distance": d} for a, b, d in dupes],
        "core_web_vitals": cwv,
        "js_render": js,
        "pages": [
            {"url": i.url, "status": i.status, "depth": i.depth, "title": i.title,
             "words": i.word_count, "ms": i.elapsed_ms}
            for i in sorted(infos, key=lambda x: (x.depth, x.url))
        ][:500],
    }
