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


def test_guarded_backend_connects_to_the_checked_ip():
    """DNS rebinding: the backend resolves once and connects to that exact IP,
    so a second, different DNS answer is never used."""
    from app.safety import BlockedTarget, GuardedBackend

    connected = []

    class FakeInner:
        async def connect_tcp(self, host, port, **kw):
            connected.append((host, port))
            return "stream"

        async def sleep(self, s):
            pass

    answers = iter(["93.184.216.34", "127.0.0.1"])  # public for the check, private afterwards

    async def resolver(host, port):
        from app.safety import _public_addresses
        return _public_addresses(host, [(None, None, None, None, (next(answers), port))])[0]

    backend = GuardedBackend(inner=FakeInner(), resolver=resolver)
    assert asyncio.run(backend.connect_tcp("rebind.test", 443)) == "stream"
    assert connected == [("93.184.216.34", 443)]
    with pytest.raises(BlockedTarget):  # the private answer is refused, not connected to
        asyncio.run(backend.connect_tcp("rebind.test", 443))
    assert len(connected) == 1


def test_guarded_backend_refuses_private_hosts():
    from app.safety import BlockedTarget, GuardedBackend, resolve_public
    with pytest.raises(BlockedTarget):
        asyncio.run(GuardedBackend().connect_tcp("localhost", 80))
    with pytest.raises(BlockedTarget):
        asyncio.run(resolve_public("127.0.0.1", 80))


def test_unfinished_audits_fail_after_restart():
    from app.store import MemoryStore, RESTART_ERROR
    s = MemoryStore()
    a, b, c = s.create("https://a.test/"), s.create("https://b.test/"), s.create("https://c.test/")
    s.update(b, "running")
    s.update(c, "done", result={})
    assert s.fail_unfinished() == 2
    assert s.get(a)["status"] == s.get(b)["status"] == "failed"
    assert s.get(a)["error"] == RESTART_ERROR
    assert s.get(c)["status"] == "done"


def test_rate_limiter_window():
    from app.ratelimit import RateLimiter
    now = [0.0]
    rl = RateLimiter(2, window_s=3600, clock=lambda: now[0])
    assert rl.retry_after("ip") == 0 and rl.retry_after("ip") == 0
    assert rl.retry_after("ip") > 0
    assert rl.retry_after("other") == 0
    now[0] = 3600
    assert rl.retry_after("ip") == 0


def test_api_rate_limit_per_ip(monkeypatch):
    from fastapi.testclient import TestClient
    from app import main
    from app.ratelimit import RateLimiter

    async def no_job(*a):
        pass
    monkeypatch.setattr(main, "_validate", lambda url: "https://example.com/")
    monkeypatch.setattr(main, "_job", no_job)
    monkeypatch.setattr(main, "limiter", RateLimiter(2))
    tc = TestClient(main.app)
    hdr = {"CF-Connecting-IP": "203.0.113.7"}
    assert tc.post("/api/audits", json={"url": "example.com"}, headers=hdr).status_code == 202
    assert tc.post("/api/audits", json={"url": "example.com"}, headers=hdr).status_code == 202
    res = tc.post("/api/audits", json={"url": "example.com"}, headers=hdr)
    assert res.status_code == 429 and int(res.headers["retry-after"]) > 0
    assert tc.post("/api/audits", json={"url": "example.com"},
                   headers={"CF-Connecting-IP": "203.0.113.8"}).status_code == 202
    form = tc.post("/start", data={"url": "example.com", "back": "/"}, headers=hdr, follow_redirects=False)
    assert form.status_code == 303 and "Too+many+audits" in form.headers["location"]


def test_compare_pdf_and_report(monkeypatch):
    from fastapi.testclient import TestClient
    from app import main
    from app.compare import compare

    async def go():
        async with client() as c:
            return await run_audit("example.com", client=c, with_vitals=False)
    new = asyncio.run(go())
    old = dict(new, health_score=new["health_score"] - 7, issues=[
        dict(new["issues"][0], pages_affected=new["issues"][0]["pages_affected"] + 3),
        {"id": "slow_response", "title": "Server response slower than 1.5 s", "level": "Warning",
         "pages_affected": 2},
    ])
    cmp = compare(old, new)
    assert cmp["health_delta"] == 7
    assert [i["id"] for i in cmp["fixed"]] == ["slow_response"]
    assert cmp["changed"][0]["delta"] == -3
    assert {i["id"] for i in cmp["new"]} == {i["id"] for i in new["issues"][1:]}

    first = main.store.create("https://example.com/")
    main.store.update(first, "done", result=old)
    second = main.store.create("https://example.com/")
    main.store.update(second, "done", result=new)
    tc = TestClient(main.app)

    page = tc.get(f"/report/{second}/").text
    assert "Since the last audit" in page and f"/report/{first}/" in page
    assert f"/report/{second}/pdf" in page and "Run this audit again" in page
    assert "Since the last audit" not in tc.get(f"/report/{first}/").text  # nothing before it

    pdf = tc.get(f"/report/{second}/pdf")
    assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF")
    assert pdf.headers["content-type"] == "application/pdf"
    assert "attachment" in pdf.headers["content-disposition"]

    running = main.store.create("https://example.com/")
    assert tc.get(f"/report/{running}/pdf").status_code == 409
    assert tc.get("/report/nope/pdf").status_code == 404
