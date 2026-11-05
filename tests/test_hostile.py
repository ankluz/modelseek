"""Model sites are untrusted: their answers must not reach the local network, leak keys or exhaust memory."""
import asyncio
import gzip
import io
import json
import threading
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest
from starlette.testclient import TestClient

from app.fetchers import HttpFetcher
from app.imagededupe import ImageDeduper
from app.main import SECURITY_HEADERS, STATIC, create_app
from app.models import ModelHit, clean_hit
from app.netguard import (MAX_RESPONSE_BYTES, BlockedRequest, NetGuard, ResponseTooLarge, is_public,
                          trusted_urls)
from app.sources import MyMiniFactory

from .test_api import CFG, H

DNS = {"evil.test": ["93.184.215.14"], "cdn.test": ["151.101.1.1"], "other.test": ["1.1.1.1"],
       "intranet.test": ["10.0.0.5"], "rebind.test": ["8.8.4.4", "127.0.0.1"], "docs.test": ["203.0.113.9"], "metadata.test": ["169.254.169.254"]}


async def fake_dns(host, port):
    return DNS.get(host, [])


def guard(*trusted):
    return NetGuard(trusted, fake_dns)


def run(coro):
    return asyncio.run(coro)


@pytest.mark.parametrize("ip,ok", [("8.8.8.8", True), ("2606:4700::1111", True), ("127.0.0.1", False), ("10.1.2.3", False),
                                   ("192.168.1.1", False), ("172.16.0.1", False), ("169.254.169.254", False),
                                   ("100.64.0.1", False), ("0.0.0.0", False), ("::1", False), ("fc00::1", False),
                                   ("fe80::1%3", False), ("::ffff:127.0.0.1", False), ("224.0.0.1", False), ("nonsense", False)])
def test_public_addresses(ip, ok):
    assert is_public(ip) is ok


@pytest.mark.parametrize("url", ["http://127.0.0.1:8088/api/history", "http://[::1]/", "http://localhost:11434/",
                                 "http://app.localhost/", "http://intranet.test/admin", "http://metadata.test/latest",
                                 "http://rebind.test/", "http://docs.test/", "file:///etc/passwd", "gopher://evil.test/", "http://:80/"])
def test_internal_and_odd_addresses_are_blocked(url):
    with pytest.raises(BlockedRequest):
        run(guard().check(url))


def test_user_configured_local_services_stay_reachable():
    g = guard("http://127.0.0.1:8089", "http://intranet.test/v1", "http://localhost:11434/v1")
    for url in ("http://127.0.0.1:8089/search?q=x", "http://intranet.test/v1/chat", "http://localhost:11434/api/tags"):
        run(g.check(url))
    with pytest.raises(BlockedRequest):
        run(g.check("http://127.0.0.1:8088/api/settings"))
    with pytest.raises(BlockedRequest):
        run(g.check("http://127.0.0.1:8089/", use_trust=False))
    run(g.check("https://evil.test/x"))


def test_trusted_urls_come_from_config():
    cfg = deepcopy(CFG)
    cfg["custom_sources"] = {"nas": {"url": "http://nas.lan:9000/api?q={q}"}}
    cfg["notify"]["url"] = "http://ha.lan:8123/hook"
    g = NetGuard(trusted_urls(cfg), fake_dns)
    assert {("nas.lan", 9000), ("ha.lan", 8123), ("127.0.0.1", 11434), ("127.0.0.1", 8089)} <= g.trusted


class Recorder:
    def __init__(self, routes):
        self.routes, self.seen = routes, []

    def __call__(self, req: httpx.Request):
        self.seen.append((str(req.url), dict(req.headers)))
        make = self.routes.get(f"{req.url.host}{req.url.path}")
        return make(req) if make else httpx.Response(404)


def fetch(routes, url, g=None, **kw):
    rec = Recorder(routes)
    g = g or guard()

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(rec), event_hooks={"request": [g.httpx_hook]}) as c:
            return await HttpFetcher(c, guard=g).get(url, **kw)

    return rec, go


def test_site_cannot_redirect_into_local_network():
    rec, go = fetch({"evil.test/s": lambda r: httpx.Response(302, headers={"Location": "http://intranet.test/admin"})},
                    "https://evil.test/s")
    with pytest.raises(BlockedRequest, match="intranet.test"):
        run(go())
    assert [u for u, _ in rec.seen] == ["https://evil.test/s"]


def test_site_cannot_redirect_to_users_own_local_service():
    g = guard("http://127.0.0.1:8089")
    rec, go = fetch({"evil.test/s": lambda r: httpx.Response(302, headers={"Location": "http://127.0.0.1:8089/search"})},
                    "https://evil.test/s", g)
    with pytest.raises(BlockedRequest):
        run(go())
    assert len(rec.seen) == 1


def test_keys_do_not_follow_redirect_to_another_site():
    routes = {"evil.test/s": lambda r: httpx.Response(302, headers={"Location": "https://other.test/collect"}),
              "other.test/collect": lambda r: httpx.Response(200, json={"ok": 1})}
    rec, go = fetch(routes, "https://evil.test/s", headers={"X-Api-Key": "k1", "Authorization": "Bearer k2"})
    assert run(go()).json() == {"ok": 1}
    first, second = rec.seen
    assert first[1]["x-api-key"] == "k1"
    assert "x-api-key" not in second[1] and "authorization" not in second[1]


def test_keys_stay_on_same_site_redirect_and_https_upgrade():
    routes = {"evil.test/a": lambda r: httpx.Response(301, headers={"Location": "https://evil.test/b"}),
              "evil.test/b": lambda r: httpx.Response(200, json={"ok": 1})}
    rec, go = fetch(routes, "http://evil.test/a", headers={"X-Api-Key": "k1"})
    run(go())
    assert all(h.get("x-api-key") == "k1" for _, h in rec.seen)


def test_redirect_loops_stop():
    rec, go = fetch({"evil.test/s": lambda r: httpx.Response(302, headers={"Location": "/s"})}, "https://evil.test/s")
    with pytest.raises(httpx.TooManyRedirects):
        run(go())
    assert len(rec.seen) == 6


def test_post_becomes_get_after_302_and_body_is_dropped():
    routes = {"evil.test/p": lambda r: httpx.Response(302, headers={"Location": "https://other.test/g"}),
              "other.test/g": lambda r: httpx.Response(200, json={"m": r.method, "len": len(r.content)})}
    rec = Recorder(routes)
    g = guard()

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(rec), event_hooks={"request": [g.httpx_hook]}) as c:
            return await HttpFetcher(c, guard=g).post_json("https://evil.test/p", {"q": "secret"}, headers={"X-Api-Key": "k"})

    assert run(go()).json() == {"m": "GET", "len": 0}


def big(req):
    return httpx.Response(200, content=b"x" * (MAX_RESPONSE_BYTES + 10))


def lying_length(req):
    return httpx.Response(200, headers={"Content-Length": str(10**12)}, content=b"{}")


def gzip_bomb(req):
    return httpx.Response(200, headers={"Content-Encoding": "gzip"}, content=gzip.compress(b"0" * (MAX_RESPONSE_BYTES * 3)))


@pytest.mark.parametrize("make", [big, lying_length, gzip_bomb])
def test_huge_answers_are_cut_off(make):
    rec, go = fetch({"evil.test/s": make}, "https://evil.test/s")
    with pytest.raises(ResponseTooLarge):
        run(go())


def test_normal_answer_and_charset_survive():
    body = "{\"t\": \"Держатель\"}".encode("cp1251")
    rec, go = fetch({"evil.test/s": lambda r: httpx.Response(200, headers={"Content-Type": "application/json; charset=windows-1251"},
                                                             content=body)}, "https://evil.test/s", params={"q": "a b"})
    r = run(go())
    assert r.json() == {"t": "Держатель"} and rec.seen[0][0] == "https://evil.test/s?q=a+b"


def test_clean_hit_limits_everything():
    h = clean_hit(ModelHit("s", "1" * 999, "Benchy‮gpj.exe\x00" + "A" * 5000, " https://evil.test/m ",
                           thumbnail="javascript:alert(1)", author={"name": "x"}, likes=float("inf"), downloads=-5,
                           makes=True, price=float("nan"), currency="EUR" * 10, license="L" * 999,
                           tags=["ok", 7, None, {"x": 1}] + ["t"] * 500, commercial="yes", position=-1))
    assert h.url == "https://evil.test/m" and h.thumbnail is None and h.author is None
    assert len(h.title) == 300 and "‮" not in h.title and "\x00" not in h.title
    assert len(h.id) == 200 and len(h.license) == 120 and len(h.currency) == 8
    assert (h.likes, h.downloads, h.makes, h.price, h.commercial, h.position) == (None, None, None, None, None, 0)
    assert h.tags[:2] == ["ok", "7"] and len(h.tags) == 40
    for bad in ("javascript:alert(1)", "data:text/html,x", "https://a.test/\"onmouseover=x", "//evil.test/x", 5, None):
        assert clean_hit(ModelHit("s", "1", "t", bad)) is None
    ok = clean_hit(ModelHit("s", "1", "t", "https://a.test/", thumbnail="data:image/png;base64,iVBORw0KGgo="))
    assert ok.thumbnail.startswith("data:image/png")
    assert clean_hit(ModelHit("s", "1", "t", "https://a.test/", thumbnail="data:image/svg+xml;base64,PHN2Zz4=")).thumbnail is None


def test_infinite_price_from_known_source_is_dropped():
    raw = json.loads('{"id": 1, "name": "Benchy", "price": 1e999, "likes": 1e999, "url": "https://mmf.test/1"}')
    h = clean_hit(MyMiniFactory.parse(raw, 0))
    assert h.price is None and h.likes is None


EVIL_ITEMS = """{"items": [
 {"id": "1", "title": "Benchy one", "url": "https://evil.test/m/1", "image": "http://intranet.test/thumb.png", "price": 1e999},
 {"id": "2", "title": "Benchy two", "url": "javascript:alert(1)", "image": "https://cdn.test/2.png"},
 {"id": "3", "title": "Benchy three", "url": "http://127.0.0.1:8088/api/history", "image": "https://cdn.test/3.png"},
 {"id": "4", "title": "Benchy four", "url": "https://evil.test/m/4", "image": "http://127.0.0.1:8089/x.png", "likes": NaN},
 {"id": "5", "title": "Benchy five", "url": "https://evil.test/m/5", "image": "https://cdn.test/5.png"}
]}"""


def hostile_app(tmp_path, seen, **custom):
    cfg = deepcopy(CFG)
    cfg["custom_sources"] = {"evil": {"url": "https://evil.test/search?q={q}", "items_path": "items",
                                      "fields": {"thumbnail": "image", "price": "price", "likes": "likes"}, **custom}}
    cfg["ranking"]["dedupe_images"] = True

    def handler(req):
        seen.append(str(req.url))
        if req.url.host == "evil.test":
            return httpx.Response(200, content=EVIL_ITEMS.encode(), headers={"Content-Type": "application/json"})
        return httpx.Response(404)

    return TestClient(create_app(cfg, transport=httpx.MockTransport(handler), db_path=":memory:", start_scheduler=False,
                                 data_dir=str(tmp_path), resolver=fake_dns))


def test_search_with_hostile_answers_stays_valid_and_local(tmp_path):
    seen = []
    with hostile_app(tmp_path, seen) as c:
        r = c.get("/api/search", params={"q": "benchy", "sources": "evil"})
        assert r.status_code == 200
        data = json.loads(r.text, parse_constant=lambda name: pytest.fail(f"{name} in JSON"))
        res = {h["id"]: h for h in data["results"]}
        assert set(res) == {"1", "2", "4", "5"}
        assert res["2"]["url"] == "https://evil.test/javascript:alert(1)"
        assert res["1"]["thumbnail"] is None and res["1"]["price"] is None
        assert res["4"]["thumbnail"] is None and res["4"]["likes"] is None
        assert res["5"]["thumbnail"] == "https://cdn.test/5.png"
        assert data["sources"][0]["dropped"] == 1
        stream = c.get("/api/search/stream", params={"q": "benchy", "sources": "evil"}).text
        assert "Infinity" not in stream and "NaN" not in stream
    assert not [u for u in seen if "intranet" in u or "127.0.0.1" in u]


def test_users_own_local_source_may_show_its_local_images(tmp_path):
    cfg = deepcopy(CFG)
    cfg["custom_sources"] = {"nas": {"url": "http://intranet.test/search?q={q}", "items_path": "items",
                                     "fields": {"thumbnail": "image"}}}
    body = {"items": [{"id": "1", "title": "Benchy", "url": "http://intranet.test/m/1", "image": "http://intranet.test/1.png"},
                      {"id": "2", "title": "Benchy 2", "url": "http://intranet.test/m/2", "image": "http://intranet.test:81/x.png"}]}
    app = create_app(cfg, transport=httpx.MockTransport(lambda r: httpx.Response(200, json=body)), db_path=":memory:",
                     start_scheduler=False, data_dir=str(tmp_path), resolver=fake_dns)
    with TestClient(app) as c:
        res = {h["id"]: h for h in c.get("/api/search", params={"q": "benchy", "sources": "nas"}).json()["results"]}
    assert res["1"]["thumbnail"] == "http://intranet.test/1.png" and res["2"]["thumbnail"] is None


def test_image_dedupe_does_not_download_from_local_network():
    pytest.importorskip("PIL")
    seen = []

    def handler(req):
        seen.append(req.url.host)
        return httpx.Response(200, content=b"x" * (MAX_RESPONSE_BYTES))

    g = guard()

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler), event_hooks={"request": [g.httpx_hook]}) as c:
            d = ImageDeduper(c, True)
            return [await d._hash(u) for u in ("http://intranet.test/a.png", "http://127.0.0.1:8088/api/x",
                                               "https://cdn.test/huge.png", "file:///etc/passwd")]

    assert run(go()) == [None, None, None, None]
    assert seen == ["cdn.test"]


def test_image_dedupe_skips_pixel_bombs():
    PIL = pytest.importorskip("PIL.Image")
    buf = io.BytesIO()
    PIL.new("1", (12000, 12000)).save(buf, "PNG")
    with pytest.raises(ValueError):
        ImageDeduper.dhash(buf.getvalue())
    small = io.BytesIO()
    PIL.new("RGB", (64, 64), "red").save(small, "JPEG")
    assert isinstance(ImageDeduper.dhash(small.getvalue()), int)


def test_page_is_served_with_protective_headers(tmp_path):
    seen = []
    with hostile_app(tmp_path, seen) as c:
        for path in ("/", "/api/sources", "/static/app.js", "/nope"):
            r = c.get(path)
            for k, v in SECURITY_HEADERS.items():
                assert r.headers.get(k) == v, (path, k)
        assert c.get("/", headers={"Host": "evil.example"}).headers["X-Frame-Options"] == "DENY"
    csp = SECURITY_HEADERS["Content-Security-Policy"]
    assert "script-src 'self';" in csp + ";" and "connect-src 'self'" in csp and "frame-ancestors 'none'" in csp


def test_page_has_no_inline_scripts():
    import re
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", html)
    assert not re.search(r"\son[a-z]+\s*=", html)


def local_site(handler_fn):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            handler_fn(self)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def test_impersonate_mode_has_the_same_limits():
    pytest.importorskip("curl_cffi")
    from app.fetchers import ImpersonateFetcher
    hits = []

    def inner(h):
        hits.append(h.path)
        h.send_response(200)
        h.end_headers()
        h.wfile.write(b"secret")

    inner_srv, inner_url = local_site(inner)

    def evil(h):
        if h.path.startswith("/redirect"):
            h.send_response(302)
            h.send_header("Location", f"{inner_url}/admin")
            h.end_headers()
        else:
            h.send_response(200)
            h.end_headers()
            h.wfile.write(b"x" * (MAX_RESPONSE_BYTES + 1))

    evil_srv, evil_url = local_site(evil)

    async def go():
        f = ImpersonateFetcher(10, guard=NetGuard([evil_url]))
        try:
            with pytest.raises(BlockedRequest):
                await f.get(f"{evil_url}/redirect")
            with pytest.raises(ResponseTooLarge):
                await f.get(f"{evil_url}/big")
            with pytest.raises(BlockedRequest):
                await f.get(f"{inner_url}/direct")
        finally:
            await f.aclose()

    try:
        run(go())
    finally:
        inner_srv.shutdown()
        evil_srv.shutdown()
    assert hits == []


def test_source_test_endpoint_reports_blocked_redirect(tmp_path):
    seen = []

    def handler(req):
        seen.append(str(req.url))
        return httpx.Response(302, headers={"Location": "http://intranet.test/admin"})

    app = create_app(deepcopy(CFG), transport=httpx.MockTransport(handler), db_path=":memory:", start_scheduler=False,
                     data_dir=str(tmp_path), resolver=fake_dns)
    with TestClient(app) as c:
        r = c.post("/api/sources/test", headers=H, json={"name": "t", "config": {"url": "https://evil.test/s?q={q}"}})
    assert r.json()["ok"] is False and "intranet.test" in r.json()["error"]
    assert seen == ["https://evil.test/s?q=benchy"]
