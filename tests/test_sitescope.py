"""Tests run against a fake website served by httpx.MockTransport (no network)."""
import asyncio
import os

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


CF_EDGE = "172.70.1.1"  # inside Cloudflare's 172.64.0.0/13


def test_client_ip_ignores_forged_cloudflare_header():
    from starlette.requests import Request
    from app.ratelimit import client_ip

    def req(peer, **headers):
        raw = [(k.replace("_", "-").lower().encode(), v.encode()) for k, v in headers.items()]
        return Request({"type": "http", "headers": raw, "client": (peer, 1)})

    # Through Cloudflare: the visitor IP comes from CF-Connecting-IP.
    assert client_ip(req("127.0.0.1", x_real_ip=CF_EDGE, cf_connecting_ip="203.0.113.7")) == "203.0.113.7"
    # Direct hit on the VM IP with a forged header: keyed by the real edge address.
    assert client_ip(req("127.0.0.1", x_real_ip="8.8.4.4", cf_connecting_ip="1.2.3.4")) == "8.8.4.4"
    # uvicorn --proxy-headers sets request.client from X-Forwarded-For, which the visitor
    # controls; a Cloudflare address there must not unlock CF-Connecting-IP.
    assert client_ip(req(CF_EDGE, x_real_ip="8.8.4.4", cf_connecting_ip="1.2.3.4")) == "8.8.4.4"
    # Without Nginx (local runs) the direct peer is used.
    assert client_ip(req("8.8.4.4")) == "8.8.4.4"
    # Garbage in the header falls back to the edge.
    assert client_ip(req("127.0.0.1", x_real_ip=CF_EDGE, cf_connecting_ip="not-an-ip")) == CF_EDGE


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
    hdr = {"X-Real-IP": CF_EDGE, "CF-Connecting-IP": "203.0.113.7"}
    assert tc.post("/api/audits", json={"url": "example.com"}, headers=hdr).status_code == 202
    assert tc.post("/api/audits", json={"url": "example.com"}, headers=hdr).status_code == 202
    res = tc.post("/api/audits", json={"url": "example.com"}, headers=hdr)
    assert res.status_code == 429 and int(res.headers["retry-after"]) > 0
    assert tc.post("/api/audits", json={"url": "example.com"},
                   headers={"X-Real-IP": CF_EDGE, "CF-Connecting-IP": "203.0.113.8"}).status_code == 202
    form = tc.post("/start", data={"url": "example.com", "back": "/"}, headers=hdr, follow_redirects=False)
    assert form.status_code == 303 and "Too+many+audits" in form.headers["location"]
    # Forging a fresh CF-Connecting-IP without coming through Cloudflare doesn't reset the limit.
    direct = {"X-Real-IP": "8.8.4.4"}
    for fake in ("10.0.0.1", "10.0.0.2"):
        assert tc.post("/api/audits", json={"url": "example.com"},
                       headers={**direct, "CF-Connecting-IP": fake}).status_code == 202
    assert tc.post("/api/audits", json={"url": "example.com"},
                   headers={**direct, "CF-Connecting-IP": "10.0.0.3"}).status_code == 429


@pytest.mark.skipif(not os.getenv("TEST_DATABASE_URL"), reason="set TEST_DATABASE_URL to test against Postgres")
def test_postgres_rate_limiter_survives_restart():
    import psycopg
    from app.ratelimit import PostgresRateLimiter

    dsn = os.environ["TEST_DATABASE_URL"]
    rl = PostgresRateLimiter(dsn, limit=2, window_s=3600)
    rl.init()
    with psycopg.connect(dsn, autocommit=True) as c:
        c.execute("DELETE FROM rate_hits WHERE key LIKE 'test-%'")
    assert rl.retry_after("test-ip") == 0 and rl.retry_after("test-ip") == 0
    # A new limiter object stands in for a restarted app: the count is still there.
    rl2 = PostgresRateLimiter(dsn, limit=2, window_s=3600)
    wait = rl2.retry_after("test-ip")
    assert 3500 < wait <= 3601
    assert rl2.retry_after("test-other") == 0
    # Hits older than the window stop counting.
    with psycopg.connect(dsn, autocommit=True) as c:
        c.execute("UPDATE rate_hits SET at = now() - interval '2 hours' WHERE key='test-ip'")
    assert rl2.retry_after("test-ip") == 0


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


def test_password_hash_roundtrip():
    from app import accounts as acc
    h = acc.hash_password("correct horse")
    assert h.startswith("scrypt$") and "correct horse" not in h
    assert acc.check_password("correct horse", h)
    assert not acc.check_password("wrong", h)
    assert not acc.check_password("x", "garbage")


def _fresh_accounts(monkeypatch):
    from fastapi.testclient import TestClient
    from app import accounts as acc, config, main, mailer
    from app.ratelimit import RateLimiter
    from app.store import MemoryStore

    sent = []

    async def fake_send(to, subject, body):
        sent.append((to, subject, body))
        return True
    monkeypatch.setattr(config, "COOKIE_SECURE", False)
    monkeypatch.setattr(mailer, "enabled", lambda: True)
    monkeypatch.setattr(mailer, "send", fake_send)
    monkeypatch.setattr(main, "accounts", acc.MemoryAccounts())
    monkeypatch.setattr(main, "store", MemoryStore())
    monkeypatch.setattr(main, "limiter", RateLimiter(100))
    monkeypatch.setattr(main, "auth_limiter", RateLimiter(100))
    monkeypatch.setattr(main, "_validate", lambda url: "https://example.com/")
    return TestClient(main.app), main, sent


def test_signup_verify_monitor_and_score_drop_alert(monkeypatch):
    import re
    tc, main, sent = _fresh_accounts(monkeypatch)

    assert tc.get("/account/", follow_redirects=False).headers["location"] == "/account/login/"
    r = tc.post("/account/signup", data={"email": "Me@Example.com ", "password": "longenough"},
                follow_redirects=False)
    assert r.status_code == 303 and "ss_session" in r.cookies
    token = re.search(r"token=([\w-]+)", sent[0][2]).group(1)
    assert sent[0][0] == "me@example.com"
    assert "Email+confirmed" in tc.get(f"/account/verify/?token={token}", follow_redirects=False).headers["location"]
    assert "not+valid" in tc.get(f"/account/verify/?token={token}", follow_redirects=False).headers["location"]
    assert main.accounts.user_by_email("me@example.com")["verified"]

    # duplicate sign-up and bad login are refused
    assert "already" in tc.post("/account/signup", data={"email": "me@example.com", "password": "longenough"},
                                follow_redirects=False).headers["location"]
    assert "Wrong" in tc.post("/account/login", data={"email": "me@example.com", "password": "nope-nope"},
                              follow_redirects=False).headers["location"]

    # cross-site form posts are refused
    assert tc.post("/account/sites", data={"url": "example.com"}, headers={"Origin": "https://evil.test"},
                   follow_redirects=False).headers["location"] == "/account/login/"

    assert "Site+added" in tc.post("/account/sites", data={"url": "example.com", "frequency": "daily"},
                                   follow_redirects=False).headers["location"]
    page = tc.get("/account/").text
    assert "https://example.com/" in page and "Daily" in page

    scores = iter([90, 80])

    async def fake_job(audit_id, url, max_pages):
        main.store.update(audit_id, "done", result={"health_score": next(scores)})
    monkeypatch.setattr(main, "_job", fake_job)

    async def run():
        return await main.run_due_monitors()
    import asyncio
    assert asyncio.run(run()) == 1          # first audit: score 90, nothing to compare
    assert asyncio.run(run()) == 0          # not due again until tomorrow
    m = main.accounts.monitors_for(main.accounts.user_by_email("me@example.com")["id"])[0]
    assert m["last_score"] == 90
    tc.post("/account/sites/%s/run" % m["id"], follow_redirects=False)
    assert asyncio.run(run()) == 1          # score 80: a 10-point drop
    alert = sent[-1]
    assert alert[0] == "me@example.com" and "fell from 90 to 80" in alert[1] and "/report/" in alert[2]


def test_logout_and_delete_account(monkeypatch):
    tc, main, _ = _fresh_accounts(monkeypatch)
    tc.post("/account/signup", data={"email": "a@b.co", "password": "longenough"})
    tc.post("/account/sites", data={"url": "example.com"})
    assert "Wrong+password" in tc.post("/account/delete", data={"password": "bad-pass"},
                                       follow_redirects=False).headers["location"]
    tc.post("/account/delete", data={"password": "longenough"})
    assert main.accounts.user_by_email("a@b.co") is None and not main.accounts.monitors
    tc.post("/account/signup", data={"email": "c@d.co", "password": "longenough"})
    tc.post("/account/logout")
    assert tc.get("/account/", follow_redirects=False).headers["location"] == "/account/login/"
    assert "Disallow: /account/" in tc.get("/robots.txt").text


@pytest.mark.skipif(not os.getenv("TEST_DATABASE_URL"), reason="set TEST_DATABASE_URL to test against Postgres")
def test_postgres_accounts():
    import psycopg
    from app import accounts as acc
    dsn = os.environ["TEST_DATABASE_URL"]
    with psycopg.connect(dsn, autocommit=True) as c:
        c.execute("DROP TABLE IF EXISTS monitors, sessions, users")
    a = acc.PostgresAccounts(dsn)
    a.init()
    uid = a.create_user("pg@example.com", acc.hash_password("pw123456"), acc.token_hash("tok"))
    assert uid and a.create_user("pg@example.com", "x", None) is None
    assert a.verify(acc.token_hash("tok")) and not a.verify(acc.token_hash("tok"))
    a.create_session(uid, "th1")
    assert a.session_user("th1")["email"] == "pg@example.com"
    mid = a.add_monitor(uid, "https://example.com/", "weekly")
    assert a.add_monitor(uid, "https://example.com/", "daily") is None
    due = a.claim_due(5)
    assert [d["id"] for d in due] == [mid] and due[0]["verified"] and a.claim_due(5) == []
    a.set_result(mid, "aud1", 77)
    assert a.monitors_for(uid)[0]["last_score"] == 77
    assert a.run_now(uid, mid) and a.claim_due(5)[0]["last_score"] == 77
    a.delete_user(uid)
    assert a.session_user("th1") is None and a.monitors_for(uid) == []
