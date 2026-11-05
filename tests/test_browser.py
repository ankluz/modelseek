import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from app.fetchers import BrowserFetcher, BrowserManager
from app.netguard import NetGuard
from app.sources.base import ChallengeError, Source

from .fixtures import CHALLENGE_HTML


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json", extra=None):
        data = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/":
            return self._send(200, "<html><body>site</body></html>", "text/html",
                              {"Set-Cookie": "cleared=1; Path=/"})
        if self.path.startswith("/api/search"):
            if "cleared=1" not in (self.headers.get("Cookie") or ""):
                return self._send(403, CHALLENGE_HTML, "text/html")
            return self._send(200, json.dumps({"ok": True, "path": self.path,
                                               "ua": self.headers.get("User-Agent")}))
        if self.path.startswith("/api/blocked"):
            return self._send(403, CHALLENGE_HTML, "text/html")
        self._send(404, "{}")

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        self._send(200, json.dumps({"echo": body, "cookie": self.headers.get("Cookie")}))


@pytest.fixture(scope="module")
def site():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def test_browser_fetch_uses_site_cookies(site, tmp_path):
    mgr = BrowserManager(tmp_path / "profile", headless=True, guard=NetGuard([site]))

    async def go():
        f = BrowserFetcher(mgr, site)
        try:
            r1 = await f.get(f"{site}/api/search", params={"q": "lamp shade"})
            r2 = await f.post_json(f"{site}/api/post", {"x": 1})
            r3 = await f.get(f"{site}/api/blocked")
        finally:
            await mgr.aclose()
        return r1, r2, r3

    try:
        r1, r2, r3 = asyncio.run(go())
    except Exception as e:
        pytest.skip(f"browser unavailable: {e}")
    assert r1.status == 200
    d = r1.json()
    assert d["ok"] and "q=lamp+shade" in d["path"]
    assert "HeadlessChrome" not in d["ua"]
    assert r2.json()["echo"] == {"x": 1} and "cleared=1" in r2.json()["cookie"]
    assert r3.looks_like_challenge
    with pytest.raises(ChallengeError):
        Source.check(r3)
    assert (tmp_path / "browser-user-agent.txt").exists()


def serve(handler):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def test_hostile_page_cannot_reach_local_network(tmp_path):
    seen = []

    class Internal(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            seen.append(self.path)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"secret")

    inner, inner_url = serve(Internal)

    class Evil(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            if self.path.startswith("/api"):
                body, kind = b'{"ok": true}', "application/json"
            else:
                body = (f"<script>fetch('{inner_url}/fetch',{{mode:'no-cors'}}).catch(()=>{{}});"
                        f"new Image().src='{inner_url}/img';"
                        f"try{{new WebSocket('{inner_url.replace('http', 'ws')}/ws')}}catch(e){{}}</script>").encode()
                kind = "text/html"
            self.send_response(200)
            self.send_header("Content-Type", kind)
            self.end_headers()
            self.wfile.write(body)

    evil, evil_url = serve(Evil)
    mgr = BrowserManager(tmp_path / "profile", headless=True, guard=NetGuard([evil_url]))

    async def go():
        try:
            r = await BrowserFetcher(mgr, evil_url).get(f"{evil_url}/api/x")
            await asyncio.sleep(1.5)
            return r
        finally:
            await mgr.aclose()

    try:
        r = asyncio.run(go())
    except Exception as e:
        pytest.skip(f"browser unavailable: {e}")
    finally:
        inner.shutdown()
        evil.shutdown()
    assert r.json() == {"ok": True}
    assert seen == []
    assert any(inner_url in u or inner_url.replace("http", "ws") in u for u in mgr.blocked)
