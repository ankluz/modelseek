"""Guests of the public profile search with their own keys, from their own browser where a site allows it,
and behind Docker's gateway they are told apart by browser instead of by address."""
import ipaddress
import json
from urllib.parse import quote

import httpx
import pytest

from app import dockernet
from app.fetchers import PlannedRequest
from app.public import Relay

from .fixtures import MMF, router
from .test_profiles import ADMIN, H, PUBLIC, make

GW = "172.18.0.1"


def recording(**kw):
    """The fake sites, plus a log of (host, full url, Authorization) for every request the server sends."""
    inner = router(**kw)
    log = []

    def handler(request: httpx.Request) -> httpx.Response:
        log.append((request.url.host, str(request.url), request.headers.get("authorization")))
        return inner.handle_request(request)

    return httpx.MockTransport(handler), log


def keys_cookie(data) -> dict:
    return {"Cookie": "ms_guest_keys=" + quote(json.dumps(data))}


def sent(log, host):
    return [entry for entry in log if entry[0] == host]


def statuses(result):
    return {s["name"]: s for s in result["sources"]}


@pytest.fixture
def setup(monkeypatch, tmp_path):
    def build(**kw):
        transport, log = recording(**kw.pop("router", {}))
        client = make(monkeypatch, tmp_path, transport=transport, **kw)
        return client, log
    return build


def test_guest_never_gets_the_owner_keys(setup):
    c, log = setup()
    with c:
        r = c.get(PUBLIC + "/api/search", params={"q": "headphone holder"}).json()
        st = statuses(r)
        for name in ("myminifactory", "thingiverse", "cults3d"):
            assert st[name]["status"] == "skipped" and "свой ключ" in st[name]["error"]
        assert not sent(log, "www.myminifactory.com") and not sent(log, "api.thingiverse.com")
        assert st["printables"]["status"] == "ok"  # no key needed: still searched by the server
        srcs = {s["name"]: s for s in c.get(PUBLIC + "/api/sources").json()["sources"]}
        assert srcs["myminifactory"]["key_fields"] == ["key_env"] and not srcs["myminifactory"]["configured"]
        assert srcs["myminifactory"]["owner_key"] is False and srcs["myminifactory"]["browser_direct"] is True
        assert "TEST_MMF_KEY" not in c.get(PUBLIC + "/api/sources").text


def test_guest_searches_with_their_own_keys(setup):
    c, log = setup()
    with c:
        mine = keys_cookie({"myminifactory": {"key_env": "guest-mmf"}, "thingiverse": {"token_env": "guest-tv"}})
        r = c.get(PUBLIC + "/api/search", params={"q": "dragon stand"}, headers=mine).json()
        st = statuses(r)
        assert st["myminifactory"]["status"] == "ok" and st["thingiverse"]["status"] == "ok"
        mmf = sent(log, "www.myminifactory.com")
        assert mmf and all("key=guest-mmf" in url and "key=k" not in url for _, url, _ in mmf)
        assert [a for *_, a in sent(log, "api.thingiverse.com")] == ["Bearer guest-tv"]
        srcs = {s["name"]: s for s in c.get(PUBLIC + "/api/sources", headers=mine).json()["sources"]}
        assert srcs["myminifactory"]["configured"] and srcs["myminifactory"]["own_keys"] == ["key_env"]
        # The owner still searches with their own key
        log.clear()
        c.get("/api/search", params={"q": "another dragon"})
        assert all("key=k" in url for _, url, _ in sent(log, "www.myminifactory.com"))


def test_guest_keys_are_checked(setup):
    c, log = setup()
    with c:
        junk = keys_cookie({"myminifactory": {"key_env": "two words", "other": "x"},
                            "thingiverse": {"token_env": "a\r\nX-Evil: 1"}, "nope": {"key_env": "z"},
                            "cults3d": "not a dict"})
        for headers in (junk, {"Cookie": "ms_guest_keys=%7Bbroken"}, {"Cookie": "ms_guest_keys=" + quote("[1,2]")}):
            r = c.get(PUBLIC + "/api/search", params={"q": "headphone holder"}, headers=headers)
            assert r.status_code == 200
            assert statuses(r.json())["myminifactory"]["status"] == "skipped"
        assert not sent(log, "www.myminifactory.com") and not sent(log, "api.thingiverse.com")


def test_owner_may_lend_keys(setup):
    c, log = setup(public={"lend_keys": True})
    with c:
        r = c.get(PUBLIC + "/api/search", params={"q": "headphone holder"}).json()
        assert statuses(r)["myminifactory"]["status"] == "ok"
        assert all("key=k" in url for _, url, _ in sent(log, "www.myminifactory.com"))
        srcs = {s["name"]: s for s in c.get(PUBLIC + "/api/sources").json()["sources"]}
        assert srcs["myminifactory"]["owner_key"] is True
        # The owner's key is never handed to a guest's browser
        assert c.get(PUBLIC + "/api/search/plan", params={"q": "dragon"}).json()["requests"] == []


def test_guest_failures_do_not_pause_a_source_for_the_owner(setup):
    c, _ = setup(router={"fail": {"www.myminifactory.com"}})
    with c:
        mine = keys_cookie({"myminifactory": {"key_env": "bad-key"}})
        for i in range(5):
            c.get(PUBLIC + "/api/search", params={"q": f"dragon {i}"}, headers=mine)
        admin = {s["name"]: s for s in c.get("/api/sources").json()["sources"]}
        assert admin["myminifactory"]["paused_until"] is None


def relayed(item_name: str, url: str = "https://www.myminifactory.com/object/77") -> str:
    data = json.loads(json.dumps(MMF))
    data["items"][0].update(id=77, name=item_name, url=url)
    return json.dumps(data)


def test_guest_browser_fetches_and_server_ranks(setup):
    c, log = setup()
    with c:
        mine = keys_cookie({"myminifactory": {"key_env": "guest-mmf"}})
        q = {"q": "dragon stand"}
        plan = c.get(PUBLIC + "/api/search/plan", params=q, headers=mine).json()
        assert plan["token"] and [p["source"] for p in plan["requests"]] == ["myminifactory"]
        req = plan["requests"][0]
        assert req["method"] == "GET" and req["url"].startswith("https://www.myminifactory.com/api/v2/search?")
        assert "key=guest-mmf" in req["url"] and req["body"] is None
        csp = c.get(PUBLIC + "/", headers=mine).headers["content-security-policy"]
        assert "connect-src 'self' https://www.myminifactory.com" in csp
        assert "connect-src 'self';" in c.get("/").headers["content-security-policy"]

        body = {"token": plan["token"], "source": "myminifactory", "status": 200, "text": relayed("Relayed dragon")}
        assert c.post(PUBLIC + "/api/search/relay", headers={**H, **mine}, json=body).json() == {"ok": True}
        again = c.post(PUBLIC + "/api/search/relay", headers={**H, **mine}, json=body)
        assert again.status_code == 409  # one answer per source

        r = c.get(PUBLIC + "/api/search", params={**q, "pf": plan["token"]}, headers=mine).json()
        st = statuses(r)["myminifactory"]
        assert st["status"] == "ok" and st.get("via") == "browser"
        assert "Relayed dragon" in {h["title"] for h in r["results"]}
        assert not sent(log, "www.myminifactory.com")  # the server never asked the site

        # Used once; the next search falls back to the server with the guest's key
        r = c.get(PUBLIC + "/api/search", params={**q, "pf": plan["token"]}, headers=mine).json()
        assert "Relayed dragon" not in {h["title"] for h in r["results"]}
        assert sent(log, "www.myminifactory.com")
        # What a guest relays is never cached for anyone else
        owner = c.get("/api/search", params=q).json()
        assert "Relayed dragon" not in {h["title"] for h in owner["results"]}


def test_relayed_answers_are_bound_and_cleaned(setup):
    c, _ = setup()
    with c:
        mine = keys_cookie({"myminifactory": {"key_env": "guest-mmf"}})
        plan = c.get(PUBLIC + "/api/search/plan", params={"q": "dragon"}, headers=mine).json()
        tok = plan["token"]

        def post(body, **headers):
            return c.post(PUBLIC + "/api/search/relay", headers={**H, **mine, **headers}, json=body)

        good = {"token": tok, "source": "myminifactory", "status": 200, "text": relayed("x")}
        assert post({**good, "source": "printables"}).status_code == 409  # not planned
        assert post({**good, "token": "nope"}).status_code == 409
        assert post(good, **{"X-Test-Peer": "10.9.9.9"}).status_code == 409  # someone else's search
        for bad in ({**good, "status": "200"}, {**good, "status": True}, {**good, "status": 99}, {**good, "text": 5}):
            assert post(bad).status_code == 400
        too_big = {**good, "text": "a" * 2_000_001}
        assert post(too_big).status_code == 413
        assert c.post(PUBLIC + "/api/login", headers=H, content=b"a" * 5000).status_code == 413  # 4 KB elsewhere

        # A link into the local network from a relayed answer is dropped like any other
        assert post({**good, "text": relayed("LAN trap", "http://192.168.1.1/admin")}).json() == {"ok": True}
        r = c.get(PUBLIC + "/api/search", params={"q": "dragon", "pf": tok}, headers=mine).json()
        assert "LAN trap" not in {h["title"] for h in r["results"]}
        assert statuses(r)["myminifactory"].get("dropped") == 1

        # A token planned for another query is ignored
        plan = c.get(PUBLIC + "/api/search/plan", params={"q": "dragon"}, headers=mine).json()
        post({**good, "token": plan["token"], "text": relayed("Wrong query")})
        r = c.get(PUBLIC + "/api/search", params={"q": "lamp", "pf": plan["token"]}, headers=mine).json()
        assert "Wrong query" not in {h["title"] for h in r["results"]}


def test_relay_store_limits():
    now = [0.0]
    relay = Relay(ttl=10, per_visitor=2, max_entries=3, clock=lambda: now[0])
    plan = {"s": PlannedRequest("GET", "https://x/1")}
    a = relay.open("v1", ("q", 1, "score"), plan)
    b = relay.open("v1", ("q", 1, "score"), plan)
    c = relay.open("v1", ("q", 1, "score"), plan)
    assert a not in relay.entries and b in relay.entries and c in relay.entries
    relay.open("v2", ("q", 1, "score"), plan)
    relay.open("v3", ("q", 1, "score"), plan)
    assert len(relay.entries) == 3
    d = relay.open("v4", ("q", 1, "score"), plan)
    assert relay.put(d, "v4", "s", 200, "{}") is None
    assert relay.take(d, "v5", ("q", 1, "score")) == {}  # not theirs
    got = relay.take(d, "v4", ("q", 1, "score"))
    assert got["s"][1].text == "{}" and got["s"][1].url == "https://x/1"
    assert relay.take(d, "v4", ("q", 1, "score")) == {}  # used once
    e = relay.open("v4", ("q", 1, "score"), plan)
    now[0] = 11
    assert relay.put(e, "v4", "s", 200, "{}") == "поиск устарел — повторите"


ROUTE = """Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\t\tMTU\tWindow\tIRTT
eth0\t00000000\t010012AC\t0003\t0\t0\t0\t00000000\t0\t0\t0
eth0\t000012AC\t00000000\t0001\t0\t0\t0\t0000FFFF\t0\t0\t0
"""
ROUTE6 = ("00000000000000000000000000000000 00 00000000000000000000000000000000 00 "
          "fd000000000000000000000000000001 00000400 00000001 00000000 00000003 eth0\n")


def test_docker_gateway_is_read_from_routes():
    gws = dockernet.parse_routes(ROUTE, ROUTE6)
    assert ipaddress.ip_address("172.18.0.1") in gws and ipaddress.ip_address("fd00::1") in gws
    assert dockernet.parse_routes("", "") == frozenset()


@pytest.fixture
def docker(monkeypatch):
    monkeypatch.setattr(dockernet, "IN_DOCKER", True)
    monkeypatch.setattr(dockernet, "gateways", lambda: frozenset({ipaddress.ip_address(GW)}))


def test_docker_admin_answers_the_gateway_only(setup, docker):
    c, _ = setup()
    with c:
        assert c.get("/api/settings", headers={"X-Test-Peer": GW}).status_code == 200
        assert c.get("/api/settings", headers={"X-Test-Peer": "127.0.0.1"}).status_code == 200
        for other in ("172.18.0.3", "10.0.0.5", "192.168.1.20"):
            assert c.get("/api/settings", headers={"X-Test-Peer": other}).status_code == 403


def as_browser(c, gid=None):
    """A request from behind Docker's gateway, as one browser (its id cookie) or a cookie-less client."""
    c.cookies.clear()
    h = {"X-Test-Peer": GW}
    if gid:
        h["Cookie"] = "ms_guest_id=" + gid
    return h


def test_docker_hidden_guests_are_told_apart_by_browser(monkeypatch, setup, docker):
    monkeypatch.setattr("app.public.FAIL_DELAY", 0)
    c, _ = setup(public={"auth": "password", "searches_per_minute": 2})
    monkeypatch.setenv("MODELSEEK_PUBLIC_PASSWORD", "pw")
    alice, bob = "a" * 24, "b" * 24
    with c:
        for _ in range(5):
            assert c.post(PUBLIC + "/api/login", headers={**H, **as_browser(c, alice)},
                          json={"password": "no"}).status_code == 403
        assert c.post(PUBLIC + "/api/login", headers={**H, **as_browser(c, alice)},
                      json={"password": "pw"}).status_code == 429
        r = c.post(PUBLIC + "/api/login", headers={**H, **as_browser(c, bob)}, json={"password": "pw"})
        assert r.status_code == 200  # one guest's typos do not lock the others out
        session = r.cookies.get("ms_public_session")

        def search(gid):
            h = as_browser(c, gid)
            h["Cookie"] = (h.get("Cookie", "") + "; ms_public_session=" + session).lstrip("; ")
            return c.get(PUBLIC + "/api/search", params={"q": "lamp"}, headers=h).status_code

        assert [search(bob), search(bob), search(bob)] == [200, 200, 429]
        assert search("c" * 24) == 200
        status = c.get("/api/settings").json()["public"]
        assert status["addresses_hidden"] is True and status["docker"] is True

        # A client that drops its cookie every time still meets an overall cap
        hits = [c.post(PUBLIC + "/api/login", headers={**H, **as_browser(c, f"{i:024d}")},
                       json={"password": "no"}).status_code for i in range(40)]
        assert 429 in hits and hits.count(403) <= 30


def test_docker_hidden_guests_cannot_pass_allow_from(setup, docker):
    c, _ = setup(public={"allow_from": ["192.168.1.0/24"]})
    with c:
        assert c.get(PUBLIC + "/api/config", headers=as_browser(c, "a" * 24)).status_code == 403
        assert c.get(PUBLIC + "/api/config", headers={"X-Test-Peer": "192.168.1.7"}).status_code == 200


def test_guest_gets_an_id_cookie(setup):
    c, _ = setup()
    with c:
        r = c.get(PUBLIC + "/")
        cookie = r.headers.get("set-cookie", "")
        assert "ms_guest_id=" in cookie and "HttpOnly" in cookie and "SameSite=strict" in cookie
        assert "ms_guest_id" not in c.get(ADMIN + "/").headers.get("set-cookie", "")
