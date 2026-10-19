"""Tests run against a fake website served by httpx.MockTransport (no network)."""
import asyncio

import httpx
import pytest

from app import checks, simhash
from app.audit import run_audit
from app.crawler import Crawler, normalise

FILLER = " ".join(f"word{i}" for i in range(250))

SITE = {
    "/robots.txt": (200, "text/plain", "User-agent: *\nDisallow: /private/\nSitemap: https://example.com/sitemap.xml\n"),
    "/sitemap.xml": (200, "application/xml",
                     '<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                     "<url><loc>https://example.com/</loc></url>"
                     "<url><loc>https://example.com/about/</loc></url>"
                     "<url><loc>https://example.com/orphan/</loc></url></urlset>"),
    "/": (200, "text/html",
          '<html lang="en"><head><title>Example home page for testing SiteScope</title>'
          '<meta name="description" content="Home"><link rel="canonical" href="https://example.com/"></head>'
          f'<body><h1>Home</h1><p>{FILLER}</p>'
          '<a href="/about/">About</a> <a href="/blog/">Blog</a> <a href="/missing/">Missing</a>'
          '<a href="/private/x">Private</a> <a href="https://external.test/dead">Dead</a>'
          '<img src="a.png"></body></html>'),
    "/about/": (200, "text/html",
                '<html><head><title>About</title></head><body><h1>About</h1><h1>Again</h1>'
                '<a href="/">Home</a></body></html>'),
    "/blog/": (200, "text/html",
               '<html lang="en"><head><title>Example home page for testing SiteScope</title>'
               '<meta name="robots" content="noindex"></head>'
               f'<body><h1>Blog</h1><p>{FILLER}</p></body></html>'),
    "/orphan/": (200, "text/html", "<html><head><title>Orphan</title></head><body>hi</body></html>"),
}


def handler(request: httpx.Request) -> httpx.Response:
    if request.url.host == "external.test":
        return httpx.Response(404)
    if request.url.host == "www.googleapis.com":
        return httpx.Response(200, json={
            "lighthouseResult": {"categories": {"performance": {"score": 0.42}},
                                 "audits": {"largest-contentful-paint": {"numericValue": 3900},
                                            "cumulative-layout-shift": {"numericValue": 0.02}}},
        })
    status, ctype, body = SITE.get(request.url.path, (404, "text/html", "not found"))
    return httpx.Response(status, headers={"content-type": ctype}, text=body)


def client():
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True)


def test_normalise():
    assert normalise("HTTPS://Example.com:443/a#frag") == "https://example.com/a"
    assert normalise("/b?x=1", "https://example.com/a/") == "https://example.com/b?x=1"
    assert normalise("mailto:x@y.z") is None
    assert normalise("https://example.com") == "https://example.com/"


def test_simhash_detects_near_duplicates():
    a = simhash.simhash(FILLER + " extra")
    b = simhash.simhash(FILLER + " other")
    c = simhash.simhash("completely different text about cooking pasta and tomato sauce " * 20)
    assert simhash.distance(a, b) <= 3
    assert simhash.distance(a, c) > 10


def test_crawler_respects_robots_and_finds_pages():
    async def go():
        async with client() as c:
            return await Crawler("https://example.com/", client=c).run()
    res = asyncio.run(go())
    assert "https://example.com/about/" in res.pages
    assert "https://example.com/private/x" in res.robots_blocked
    assert "https://example.com/private/x" not in res.pages
    assert "https://example.com/orphan/" in res.sitemap_urls
    assert res.link_status["https://external.test/dead"] == 404


def test_full_audit_finds_expected_issues():
    async def go():
        async with client() as c, client() as psi:
            return await run_audit("example.com", client=c, psi_client=psi)
    r = asyncio.run(go())
    ids = {i["id"]: i for i in r["issues"]}
    assert "client_error" in ids                      # /missing/ is 404
    assert "noindex" in ids                           # /blog/
    assert "broken_internal_link" in ids              # home links to /missing/
    assert "broken_external_link" in ids              # external.test/dead
    assert "multiple_h1" in ids                       # /about/
    assert "duplicate_title" in ids                   # home and blog share a title
    assert "orphan_page" in ids                       # /orphan/ only in sitemap
    assert "near_duplicate" in ids                    # home and blog share text
    assert "missing_alt" in ids
    assert "slow_lcp" in ids                          # lab LCP 3900 ms
    assert r["core_web_vitals"]["performance_score"] == 42
    # ranked by priority, highest first
    prios = [i["priority"] for i in r["issues"]]
    assert prios == sorted(prios, reverse=True)
    assert 0 <= r["health_score"] <= 100


def test_priority_formula():
    ranked = checks.prioritise({"server_error": {"a": "", "b": ""}, "missing_alt": {"a": ""}}, total_pages=4)
    by = {r["id"]: r for r in ranked}
    assert by["server_error"]["priority"] == 150.0    # 3 x 50%
    assert by["missing_alt"]["priority"] == 25.0      # 1 x 25%
    assert ranked[0]["id"] == "server_error"


def test_ssrf_guard_blocks_private_targets():
    from app.safety import BlockedTarget, check_url
    for bad in ("http://127.0.0.1/", "http://localhost/", "http://169.254.169.254/latest/meta-data/", "ftp://example.com/"):
        with pytest.raises(BlockedTarget):
            check_url(bad)


def test_web_pages_and_api():
    from fastapi.testclient import TestClient
    from app.main import app, PAGES
    tc = TestClient(app)
    for path in PAGES:
        res = tc.get(path)
        assert res.status_code == 200, path
        assert "<h1>" in res.text and 'rel="canonical"' in res.text
    assert "Disallow: /report/" in tc.get("/robots.txt").text
    assert "/javascript-seo-checker/" in tc.get("/sitemap.xml").text
    assert tc.post("/api/audits", json={"url": "http://127.0.0.1"}).status_code == 400
    assert tc.get("/api/audits/nope").status_code == 404


def test_head_requests_and_new_pages():
    from fastapi.testclient import TestClient
    from app.main import app
    tc = TestClient(app)
    for path in ("/", "/sitemap.xml", "/health", "/is-my-react-website-seo-friendly/"):
        res = tc.head(path)
        assert res.status_code == 200, path
        assert res.content == b"", path
    sitemap = tc.get("/sitemap.xml").text
    for path in ("/is-my-react-website-seo-friendly/", "/free-seo-audit-small-business/", "/privacy/"):
        assert path in sitemap
    home = tc.get("/").text
    assert "googletagmanager" not in home  # no GA4 tag unless GA4_ID is set
    assert '<link rel="stylesheet"' not in home and "<style>" in home  # CSS is inlined
    assert "&#34;" not in home.split("</style>")[0]  # inlined CSS is not HTML-escaped
    assert "google-site-verification" not in home


def test_analytics_tags_when_configured():
    from fastapi.testclient import TestClient
    from app.main import app, templates
    templates.env.globals.update(ga4_id="G-TEST123", gsc_verification="tok123")
    try:
        html = TestClient(app).get("/").text
        assert "gtag/js?id=G-TEST123" in html
        assert 'name="google-site-verification" content="tok123"' in html
        assert "Google Analytics 4" in TestClient(app).get("/privacy/").text
    finally:
        templates.env.globals.update(ga4_id="", gsc_verification="")


def test_pagerank_matches_networkx_reference():
    import networkx as nx
    from app.linkgraph import pagerank
    g = nx.DiGraph([("a", "b"), ("b", "c"), ("c", "a"), ("a", "c"), ("d", "c")])
    mine = pagerank(g)
    ref = nx.algorithms.link_analysis.pagerank_alg._pagerank_python(g)
    for k in ref:
        assert abs(mine[k] - ref[k]) < 1e-4
    assert abs(sum(mine.values()) - 1) < 1e-6
