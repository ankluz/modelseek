"""Two profiles in one process: the admin one for this computer and the public one for visitors."""
import io
import json
import zipfile
from copy import deepcopy

import pytest
from starlette.testclient import TestClient

from app import source
from app.config import DEFAULTS, _merge, validate_public
from app.main import create_app
from app.public import RateLimit, Sessions, create_public_app

from .fixtures import router
from .test_api import CFG

H = {"X-Modelseek": "1"}
ADMIN = "http://testserver"
PUBLIC = "http://testserver:8090"


def both(admin, public):
    """One ASGI app for the test: port 8090 goes to the public profile; X-Test-Peer sets the visitor's IP."""
    async def app(scope, receive, send):
        if scope["type"] == "lifespan":
            return await admin(scope, receive, send)
        headers = scope.get("headers") or []
        peer = dict(headers).get(b"x-test-peer")
        if peer:
            scope = {**scope, "client": (peer.decode(), 40000),
                     "headers": [(k, v) for k, v in headers if k != b"x-test-peer"]}
        port = (scope.get("server") or ("", 80))[1]
        return await (public if port == 8090 else admin)(scope, receive, send)
    return app


def make(monkeypatch, tmp_path, public=None, transport=None, **cfg_over):
    monkeypatch.setenv("TEST_TV_TOKEN", "t")
    monkeypatch.setenv("TEST_MMF_KEY", "k")
    monkeypatch.delenv("MODELSEEK_PUBLIC_PASSWORD", raising=False)
    cfg = _merge(deepcopy(CFG), {"public": {"enabled": True, **(public or {})}, **cfg_over})
    admin = create_app(cfg, transport=transport or router(), db_path=":memory:", start_scheduler=False,
                       data_dir=str(tmp_path))
    pub = create_public_app(admin.state.runtime)
    return TestClient(both(admin, pub), base_url=ADMIN)


@pytest.fixture
def c(monkeypatch, tmp_path):
    with make(monkeypatch, tmp_path) as client:
        yield client


OWNER_ONLY = [
    ("GET", "/api/settings"), ("PATCH", "/api/settings"), ("POST", "/api/settings/reset"), ("PUT", "/api/secrets"),
    ("GET", "/api/favorites"), ("POST", "/api/favorites"), ("GET", "/api/hidden"), ("POST", "/api/hidden"),
    ("POST", "/api/seen"), ("GET", "/api/history"), ("DELETE", "/api/history"), ("GET", "/api/subs"),
    ("POST", "/api/subs"), ("POST", "/api/subs/1/run"), ("POST", "/api/notify/test"), ("GET", "/api/extras"),
    ("POST", "/api/extras/install"), ("POST", "/api/extras/chromium"), ("POST", "/api/ollama/install"),
    ("GET", "/api/jobs/x"), ("POST", "/api/browser/open"), ("POST", "/api/browser/done"),
    ("POST", "/api/sources/test"), ("POST", "/api/sources/printables/reset"), ("POST", "/api/cache/clear"),
    ("POST", "/api/presets"), ("DELETE", "/api/presets/1"), ("POST", "/api/semantic/warm"),
]


@pytest.mark.parametrize("method,path", OWNER_ONLY)
def test_public_port_has_no_owner_routes(c, method, path):
    r = c.request(method, PUBLIC + path, headers=H, json={})
    assert r.status_code in (404, 405), (path, r.status_code, r.text[:200])


@pytest.fixture
def lending(monkeypatch, tmp_path):
    with make(monkeypatch, tmp_path, public={"lend_keys": True}) as client:
        yield client


def test_guest_search_does_not_see_or_change_owner_data(lending):
    c = lending
    found = c.get("/api/search", params={"q": "headphone holder"}).json()["results"]
    fav, hidden = found[0], found[1]
    c.post("/api/favorites", headers=H, json={"hit": fav, "collection": "Мои"})
    c.post("/api/hidden", headers=H, json={"key": hidden["key"], "title": hidden["title"]})
    c.post("/api/seen", headers=H, json={"key": fav["key"]})
    before = c.get("/api/history").json()["items"]

    guest = c.get(PUBLIC + "/api/search", params={"q": "headphone holder", "hide_seen": "1",
                                                  "w_personal": "1"}).json()["results"]
    keys = {h["key"] for h in guest}
    assert hidden["key"] in keys and fav["key"] in keys
    assert all(h["favorite"] is None and h["seen"] is False for h in guest)
    assert all("personal" not in (h.get("breakdown") or {}) or h["breakdown"]["personal"] == 0 for h in guest)
    assert c.get("/api/history").json()["items"] == before

    with c.stream("GET", PUBLIC + "/api/search/stream", params={"q": "lamp"}) as r:
        "".join(r.iter_text())
    assert not any(h["query"] == "lamp" for h in c.get("/api/history").json()["items"])


def test_public_config_and_sources_are_sanitized(c):
    cfg = c.get(PUBLIC + "/api/config").json()
    assert cfg["profile"] == "public" and cfg["weights"]["personal"] == 0
    assert cfg["source_url"] == "/source.zip" and cfg["intervals"] == []
    srcs = c.get(PUBLIC + "/api/sources").text
    assert "TEST_" not in srcs and "CULTS" not in srcs
    assert all(s["missing"] == [] for s in json.loads(srcs)["sources"])
    assert c.get("/api/config").json()["profile"] == "admin"
    presets = c.get(PUBLIC + "/api/presets").json()["items"]
    assert presets and all(p["builtin"] for p in presets)


def test_page_carries_the_profile(c):
    assert 'data-profile="public"' in c.get(PUBLIC + "/").text
    assert 'data-profile="admin"' in c.get("/").text
    for url in (PUBLIC + "/", PUBLIC + "/api/config"):
        assert "default-src 'self'" in c.get(url).headers["content-security-policy"]


def test_admin_answers_only_this_computer(c):
    assert c.get("/api/config", headers={"X-Test-Peer": "127.0.0.1"}).status_code == 200
    assert c.get("/api/config", headers={"X-Test-Peer": "::1"}).status_code == 200
    for peer in ("192.168.1.5", "10.0.0.2", "8.8.8.8"):
        r = c.get("/api/config", headers={"X-Test-Peer": peer})
        assert r.status_code == 403, peer
    assert c.get("/", headers={"X-Test-Peer": "192.168.1.5"}).status_code == 403
    r = c.get("/api/config", headers={"X-Test-Peer": "127.0.0.1", "X-Forwarded-For": "8.8.8.8"})
    assert r.status_code == 403
    assert c.get(PUBLIC + "/api/config", headers={"X-Test-Peer": "192.168.1.5"}).status_code == 200


def test_password_login(monkeypatch, tmp_path):
    with make(monkeypatch, tmp_path, public={"auth": "password"}) as c:
        assert c.get(PUBLIC + "/api/search", params={"q": "x"}).status_code == 503
        monkeypatch.setenv("MODELSEEK_PUBLIC_PASSWORD", "s3cret")
        r = c.get(PUBLIC + "/api/search", params={"q": "x"})
        assert r.status_code == 401 and r.json()["login"] is True
        assert c.get(PUBLIC + "/").status_code == 200
        assert c.get(PUBLIC + "/static/app.js").status_code == 200
        assert c.get(PUBLIC + "/api/session").json() == {"auth": "password", "logged_in": False, "password_set": True}

        assert c.post(PUBLIC + "/api/login", json={"password": "s3cret"}).status_code == 403  # no X-Modelseek
        evil = {**H, "Origin": "https://evil.example"}
        assert c.post(PUBLIC + "/api/login", headers=evil, json={"password": "s3cret"}).status_code == 403
        assert c.post(PUBLIC + "/api/login", headers=H, json={"password": "nope"}).status_code == 403

        r = c.post(PUBLIC + "/api/login", headers=H, json={"password": "s3cret"})
        assert r.status_code == 200 and r.json()["logged_in"]
        set_cookie = r.headers["set-cookie"].lower()
        assert "ms_public_session=" in set_cookie and "httponly" in set_cookie and "samesite=strict" in set_cookie
        assert c.get(PUBLIC + "/api/search", params={"q": "headphone"}).status_code == 200

        monkeypatch.setenv("MODELSEEK_PUBLIC_PASSWORD", "changed")
        assert c.get(PUBLIC + "/api/search", params={"q": "headphone"}).status_code == 401


def test_login_attempts_are_limited(monkeypatch, tmp_path):
    monkeypatch.setattr("app.public.asyncio.sleep", _no_sleep)
    with make(monkeypatch, tmp_path, public={"auth": "password"}) as c:
        monkeypatch.setenv("MODELSEEK_PUBLIC_PASSWORD", "s3cret")
        peer = {**H, "X-Test-Peer": "203.0.113.7"}
        codes = [c.post(PUBLIC + "/api/login", headers=peer, json={"password": f"guess{i}"}).status_code
                 for i in range(6)]
        assert codes[:5] == [403] * 5 and codes[5] == 429
        r = c.post(PUBLIC + "/api/login", headers=peer, json={"password": "s3cret"})
        assert r.status_code == 429 and int(r.headers["retry-after"]) > 0
        other = {**H, "X-Test-Peer": "203.0.113.8"}
        assert c.post(PUBLIC + "/api/login", headers=other, json={"password": "s3cret"}).status_code == 200


async def _no_sleep(*_):
    return None


def test_subnet_only(monkeypatch, tmp_path):
    with make(monkeypatch, tmp_path, public={"auth": "subnet", "allow_from": ["10.0.0.0/8", "fd00::/8"]}) as c:
        assert c.get(PUBLIC + "/", headers={"X-Test-Peer": "10.1.2.3"}).status_code == 200
        assert c.get(PUBLIC + "/api/config", headers={"X-Test-Peer": "fd00::5"}).status_code == 200
        for peer in ("192.168.1.5", "8.8.8.8", "testclient"):
            assert c.get(PUBLIC + "/", headers={"X-Test-Peer": peer}).status_code == 403, peer


def test_forwarded_for_only_from_trusted_proxy(monkeypatch, tmp_path):
    pub = {"allow_from": ["10.0.0.0/8"], "trusted_proxies": ["127.0.0.1"]}
    with make(monkeypatch, tmp_path, public=pub) as c:
        via_proxy = {"X-Test-Peer": "127.0.0.1", "X-Forwarded-For": "10.9.9.9"}
        assert c.get(PUBLIC + "/api/config", headers=via_proxy).status_code == 200
        spoofed = {"X-Test-Peer": "8.8.8.8", "X-Forwarded-For": "10.9.9.9"}
        assert c.get(PUBLIC + "/api/config", headers=spoofed).status_code == 403
        chain = {"X-Test-Peer": "127.0.0.1", "X-Forwarded-For": "10.9.9.9, 8.8.8.8"}
        assert c.get(PUBLIC + "/api/config", headers=chain).status_code == 403


def test_search_rate_limit(monkeypatch, tmp_path):
    with make(monkeypatch, tmp_path, public={"searches_per_minute": 2}) as c:
        peer = {"X-Test-Peer": "198.51.100.4"}
        codes = [c.get(PUBLIC + "/api/search", headers=peer, params={"q": "headphone"}).status_code for _ in range(3)]
        assert codes == [200, 200, 429]
        with c.stream("GET", PUBLIC + "/api/search/stream", headers=peer, params={"q": "headphone"}) as r:
            text = "".join(r.iter_text())
        assert text.startswith("event: fail") and "слишком много" in text
        other = {"X-Test-Peer": "198.51.100.5"}
        assert c.get(PUBLIC + "/api/search", headers=other, params={"q": "headphone"}).status_code == 200
        assert c.get("/api/search", params={"q": "headphone"}).status_code == 200


def test_owner_local_sources_are_not_offered_to_guests(monkeypatch, tmp_path):
    custom = {"nas": {"mode": "http", "url": "http://192.168.1.20/api?q={q}", "label": "NAS",
                      "items": "items", "fields": {"title": "name", "url": "link"}},
              "site": {"mode": "link", "search_page": "https://models.example.com/search?q={q}", "label": "Site"}}
    with make(monkeypatch, tmp_path, custom_sources=custom) as c:
        names = {s["name"] for s in c.get(PUBLIC + "/api/sources").json()["sources"]}
        assert "nas" not in names and "site" in names and "printables" in names
        st = c.get(PUBLIC + "/api/search", params={"q": "x", "sources": "nas,printables"}).json()["sources"]
        assert [s["name"] for s in st] == ["printables"]
        assert "nas" in {s["name"] for s in c.get("/api/sources").json()["sources"]}


def test_only_listed_sources(monkeypatch, tmp_path):
    with make(monkeypatch, tmp_path, public={"sources": ["printables", "yeggi"]}) as c:
        names = [s["name"] for s in c.get(PUBLIC + "/api/sources").json()["sources"]]
        assert sorted(names) == ["printables", "yeggi"]
        st = c.get(PUBLIC + "/api/search", params={"q": "headphone"}).json()["sources"]
        assert sorted(s["name"] for s in st) == ["printables", "yeggi"]


def test_admin_sees_public_status_and_can_set_the_password(c):
    s = c.get("/api/settings").json()
    assert s["public"]["enabled"] is True and s["public"]["password_set"] is False
    assert "MODELSEEK_PUBLIC_PASSWORD" in s["secrets"]
    r = c.patch("/api/settings", headers=H, json={"patch": {"public": {"auth": "none"}}})
    assert r.status_code == 400  # the public profile is configured in config.yaml only
    s = c.put("/api/secrets", headers=H, json={"values": {"MODELSEEK_PUBLIC_PASSWORD": "pw"}}).json()
    assert s["public"]["password_set"] is True


@pytest.mark.parametrize("bad", [
    {"auth": "magic"}, {"enabled": True, "auth": "subnet"}, {"allow_from": ["not-a-net"]},
    {"port": 0}, {"searches_per_minute": "lots"}, {"source_url": "javascript:alert(1)"},
])
def test_bad_public_config_stops_the_start(bad):
    with pytest.raises(ValueError):
        validate_public(_merge(DEFAULTS["public"], bad))


def test_sessions_expire_and_follow_the_password(tmp_path):
    s = Sessions(tmp_path / "k")
    t = s.issue("pw", now=1000)
    assert s.valid(t, "pw", now=2000)
    assert not s.valid(t, "other", now=2000)
    assert not s.valid(t, "pw", now=1000 + 31 * 86400)
    assert not s.valid(t[:-1] + ("0" if t[-1] != "0" else "1"), "pw", now=2000)
    assert Sessions(tmp_path / "k").key == s.key


def test_rate_limit_window():
    now = [0.0]
    rl = RateLimit(2, 60, clock=lambda: now[0])
    assert rl.hit("a") and rl.hit("a") and not rl.hit("a") and rl.hit("b")
    now[0] = 61
    assert rl.hit("a")


def test_source_archive_has_code_but_no_private_files(c, tmp_path):
    r = c.get(PUBLIC + "/source.zip")
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
    names = zipfile.ZipFile(io.BytesIO(r.content)).namelist()
    assert "modelseek/app/main.py" in names and "modelseek/LICENSE" in names and "modelseek/frontend/index.html" in names
    assert not any(n.endswith((".env", "config.yaml", "secrets.env", ".db")) or "/data/" in n for n in names)

    root = tmp_path / "proj"
    (root / "app").mkdir(parents=True)
    (root / "app" / "x.py").write_text("x = 1")
    (root / "data").mkdir()
    (root / "data" / "secrets.env").write_text("K=v")
    (root / ".env").write_text("K=v")
    (root / "config.yaml").write_text("public: {}")
    got = zipfile.ZipFile(io.BytesIO(source.archive(root))).namelist()
    assert got == ["modelseek/app/x.py"]


def test_admin_refuses_another_port_of_the_same_host(c):
    """A page on the public port (same host, other port) must not drive the admin profile."""
    for origin in ("http://127.0.0.1:8090", "http://testserver:8090", "http://testserver.evil.com", "null"):
        r = c.post("/api/cache/clear", headers={**H, "Origin": origin})
        assert r.status_code == 403, origin
    assert c.post("/api/cache/clear", headers={**H, "Origin": "http://testserver"}).status_code == 200


def test_request_bodies_are_limited(monkeypatch, tmp_path):
    def chunks():
        for _ in range(40):
            yield b"a" * 8192

    with make(monkeypatch, tmp_path, public={"auth": "password"}) as c:
        monkeypatch.setenv("MODELSEEK_PUBLIC_PASSWORD", "pw")
        r = c.post(PUBLIC + "/api/login", headers={**H, "Content-Type": "application/json"},
                   content=b'{"password": "' + b"a" * 10_000 + b'"}')
        assert r.status_code == 413
        assert c.post(PUBLIC + "/api/login", headers=H, content=chunks()).status_code == 413
        big = c.post("/api/presets", headers={**H, "Content-Type": "application/json"},
                     content=b'{"name": "' + b"a" * 400_000 + b'"}')
        assert big.status_code == 413
        assert c.post(PUBLIC + "/api/login", headers=H, json={"password": "pw"}).status_code == 200


@pytest.mark.parametrize("path", ["/static/INDEX~1.HTM", "/static/index.html::$DATA", "/static/index.html.",
                                  "/static/..%2fapp%2fmain.py", "/static/fonts/..%5c..%5capp%5cmain.py",
                                  "/static/%2e%2e/config.yaml", "/static/app.js%00"])
def test_static_takes_plain_names_only(c, path):
    for base in ("", PUBLIC):
        assert c.get(base + path).status_code == 404, base + path


def test_fonts_are_local(c):
    r = c.get(PUBLIC + "/static/fonts/OrreryText-Regular.woff")
    assert r.status_code == 200 and r.content[:4] == b"wOFF"
    assert "SIL Open Font License" in c.get("/static/fonts/OFL.txt").text
    page = c.get("/").text
    csp = c.get("/").headers["content-security-policy"]
    assert "googleapis" not in page and "gstatic" not in page and "https:" not in csp.split("img-src")[0]
    names = zipfile.ZipFile(io.BytesIO(c.get("/source.zip").content)).namelist()
    assert "modelseek/frontend/fonts/OFL.txt" in names


def test_public_http_cuts_slow_and_greedy_clients(monkeypatch):
    import socket
    import threading
    import time

    from starlette.applications import Starlette
    from starlette.responses import PlainTextResponse
    from starlette.routing import Route

    from app.serve import GuestH11, server

    monkeypatch.setattr(GuestH11, "REQUEST_TIMEOUT", 0.6)
    monkeypatch.setattr(GuestH11, "PER_ADDRESS", 5)
    app = Starlette(routes=[Route("/", lambda r: PlainTextResponse("ok"))])
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    srv = server(app, "127.0.0.1", port, connections=50, guests=True)
    t = threading.Thread(target=srv.run, daemon=True)
    t.start()
    for _ in range(100):
        if srv.started:
            break
        time.sleep(0.05)

    def closed(s):
        s.settimeout(0.02)
        try:
            return s.recv(1) == b""
        except socket.timeout:
            return False
        except OSError:
            return True

    try:
        held = []
        for _ in range(8):
            s = socket.create_connection(("127.0.0.1", port))
            s.sendall(b"GET / HTTP/1.1\r\nHost: x\r\n")
            held.append(s)
        time.sleep(0.2)
        assert sum(closed(s) for s in held) == 3  # over the per-address cap
        time.sleep(0.8)
        assert all(closed(s) for s in held)  # never finished the request
        with socket.create_connection(("127.0.0.1", port)) as s:
            s.sendall(b"GET / HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
            assert s.recv(1024).startswith(b"HTTP/1.1 200")
    finally:
        for s in held:
            s.close()
        srv.should_exit = True
        t.join(5)
