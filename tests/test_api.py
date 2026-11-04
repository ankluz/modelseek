import json
from copy import deepcopy

import pytest
from starlette.testclient import TestClient

from app.config import DEFAULTS, _merge
from app.main import create_app

from .fixtures import router

H = {"X-Modelseek": "1"}

CFG = _merge(DEFAULTS, {
    "server": {"allowed_hosts": ["testserver"]},
    "sources": {
        "printables": {"enabled": True, "weight": 1.0, "mode": "http"},
        "makerworld": {"enabled": True, "weight": 1.0, "mode": "http"},
        "thingiverse": {"enabled": True, "weight": 0.8, "token_env": "TEST_TV_TOKEN"},
        "thangs": {"enabled": True, "weight": 0.7, "mode": "http"},
        "cults3d": {"enabled": True, "user_env": "TEST_CULTS_USER", "key_env": "TEST_CULTS_KEY"},
        "myminifactory": {"enabled": True, "key_env": "TEST_MMF_KEY"},
        "searxng": {"enabled": False},
    },
    "links": {"yeggi": {"label": "Yeggi", "url": "https://www.yeggi.com/q/{q}/"}},
})


def make(monkeypatch, cfg=None, data_dir=None, **router_kw):
    import tempfile
    monkeypatch.setenv("TEST_TV_TOKEN", "t")
    monkeypatch.setenv("TEST_MMF_KEY", "k")
    return TestClient(create_app(deepcopy(cfg or CFG), transport=router(**router_kw), db_path=":memory:",
                                 start_scheduler=False, data_dir=data_dir or tempfile.mkdtemp()))


@pytest.fixture
def client(monkeypatch, tmp_path):
    with make(monkeypatch, data_dir=str(tmp_path)) as c:
        yield c


def test_search_merges_all_sources(client):
    data = client.get("/api/search", params={"q": "headphone holder"}).json()
    st = {s["name"]: s for s in data["sources"]}
    assert st["printables"]["status"] == "ok" and st["printables"]["count"] == 2
    assert st["cults3d"]["status"] == "skipped"
    assert st["yeggi"]["status"] == "link" and st["yeggi"]["link"] == "https://www.yeggi.com/q/headphone+holder/"
    assert "searxng" not in st
    top = data["results"][0]
    assert "headphone" in top["title"].lower()
    assert "semantic" not in top["breakdown"]
    assert set(top["breakdown"]) >= {"relevance", "popularity", "freshness", "source_rank", "source_weight"}


def test_stream_sends_updates_then_end(client):
    with client.stream("GET", "/api/search/stream", params={"q": "headphone"}) as r:
        text = "".join(r.iter_text())
    events = [e for e in text.split("\n\n") if e.strip()]
    updates = [json.loads(e.split("data: ", 1)[1]) for e in events if e.startswith("event: update")]
    assert events[-1].startswith("event: end")
    assert updates and updates[-1]["done"] is True
    assert updates[-1]["results"] and "facets" in updates[-1]


def test_engine_streams_as_sources_answer():
    import asyncio

    import httpx

    from app.engine import Engine
    from app.sources import build_sources
    from .fixtures import router as sync_router

    inner = sync_router()

    async def handler(req):
        if req.url.host == "makerworld.com":
            await asyncio.sleep(0.3)
        return inner.handler(req)

    async def go():
        cfg = deepcopy(CFG)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            eng = Engine(build_sources(cfg), c, cfg)
            snaps = [(len(h), [s["status"] for s in st], done)
                     async for h, st, done in eng.stream("headphone", ["printables", "makerworld"])]
            await eng.aclose()
            return snaps

    snaps = asyncio.run(go())
    assert len(snaps) == 2
    assert snaps[0] == (2, ["ok", "pending"], False)
    assert snaps[1][2] is True and snaps[1][0] == 4


def test_link_mode_does_not_call_site(monkeypatch):
    cfg = deepcopy(CFG)
    cfg["sources"]["makerworld"]["mode"] = "link"
    calls = []
    with make(monkeypatch, cfg, calls=calls) as c:
        data = c.get("/api/search", params={"q": "lamp"}).json()
    st = {s["name"]: s for s in data["sources"]}
    assert st["makerworld"]["status"] == "link"
    assert st["makerworld"]["link"] == "https://makerworld.com/en/search/models?keyword=lamp"
    assert "makerworld.com" not in calls


def test_challenge_and_breaker(monkeypatch):
    with make(monkeypatch, challenge={"makerworld.com"}) as c:
        for i in range(3):
            data = c.get("/api/search", params={"q": f"x{i}"}).json()
        st = {s["name"]: s for s in data["sources"]}
        assert st["makerworld"]["status"] == "challenge"
        assert st["makerworld"]["can_open"] is False
        data = c.get("/api/search", params={"q": "x9"}).json()
        st = {s["name"]: s for s in data["sources"]}
        assert st["makerworld"]["status"] == "paused"
        assert st["printables"]["status"] == "ok"
        assert c.post("/api/sources/makerworld/reset", headers=H).json()["ok"]
        data = c.get("/api/search", params={"q": "x10"}).json()
        assert {s["name"]: s for s in data["sources"]}["makerworld"]["status"] == "challenge"


def test_duplicate_model_is_merged(client):
    data = client.get("/api/search", params={"q": "headphone holder desk clamp"}).json()
    clamp = [h for h in data["results"] if h["title"].lower() == "headphone holder desk clamp"]
    assert len(clamp) == 1 and len(clamp[0]["also_on"]) == 1
    data = client.get("/api/search", params={"q": "headphone holder desk clamp", "dedupe": "0"}).json()
    assert len([h for h in data["results"] if h["title"].lower() == "headphone holder desk clamp"]) == 2


def test_filters_via_params(client):
    base = client.get("/api/search", params={"q": "headphone"}).json()["results"]
    data = client.get("/api/search", params={"q": "headphone", "price": "free"}).json()
    assert all(h["is_free"] is not False for h in data["results"])
    data = client.get("/api/search", params={"q": "headphone", "exclude": "dragon,skull"}).json()
    assert not any(w in h["title"].lower() for h in data["results"] for w in ("dragon", "skull"))
    data = client.get("/api/search", params={"q": "headphone", "include": "clamp"}).json()
    assert data["results"] and all("clamp" in h["title"].lower() for h in data["results"])
    data = client.get("/api/search", params={"q": "headphone", "min_makes": "5"}).json()
    assert all((h["makes"] or 0) >= 5 for h in data["results"]) and len(data["results"]) < len(base)
    data = client.get("/api/search", params={"q": "headphone", "license": "commercial_strict"}).json()
    assert all(h["commercial"] is True for h in data["results"])
    data = client.get("/api/search", params={"q": "headphone", "exclude_authors": "maker_anna"}).json()
    assert not any(h["author"] == "maker_anna" for h in data["results"])
    assert client.get("/api/search", params={"q": ""}).status_code == 400


def test_source_selection_and_weights(client):
    data = client.get("/api/search", params={"q": "headphone", "sources": "thingiverse,printables"}).json()
    assert {s["name"] for s in data["sources"]} == {"thingiverse", "printables"}
    data = client.get("/api/search", params={"q": "headphone holder", "sw_printables": "0", "sw_makerworld": "0",
                                             "sw_thangs": "0", "sw_myminifactory": "0"}).json()
    assert data["results"][0]["source"] == "thingiverse"


def test_cache_hit_on_reweight(client):
    client.get("/api/search", params={"q": "headphone"})
    data = client.get("/api/search", params={"q": "headphone", "w_freshness": "5"}).json()
    assert all(s.get("cached") for s in data["sources"] if s["status"] == "ok")


def test_csrf_guard(client):
    assert client.post("/api/cache/clear").status_code == 403
    assert client.post("/api/cache/clear", headers=H).status_code == 200


def test_favorites_hidden_seen_history(client):
    hit = client.get("/api/search", params={"q": "headphone"}).json()["results"][0]
    assert client.post("/api/favorites", json={"hit": hit, "collection": "Стол"}, headers=H).json()["ok"]
    favs = client.get("/api/favorites").json()
    assert favs["items"][0]["key"] == hit["key"] and favs["collections"] == [{"name": "Стол", "count": 1}]
    client.patch("/api/favorites", json={"key": hit["key"], "note": "печатать PETG"}, headers=H)
    assert client.get("/api/favorites", params={"collection": "Стол"}).json()["items"][0]["note"] == "печатать PETG"
    res = client.get("/api/search", params={"q": "headphone"}).json()["results"]
    assert next(h for h in res if h["key"] == hit["key"])["favorite"] == "Стол"

    client.post("/api/hidden", json={"key": hit["key"], "title": hit["title"]}, headers=H)
    res = client.get("/api/search", params={"q": "headphone"}).json()["results"]
    assert hit["key"] not in {h["key"] for h in res}
    client.delete("/api/hidden", params={"key": hit["key"]}, headers=H)

    client.post("/api/seen", json={"key": hit["key"]}, headers=H)
    res = client.get("/api/search", params={"q": "headphone"}).json()["results"]
    assert next(h for h in res if h["key"] == hit["key"])["seen"] is True
    res = client.get("/api/search", params={"q": "headphone", "hide_seen": "1"}).json()["results"]
    assert hit["key"] not in {h["key"] for h in res}

    hist = client.get("/api/history").json()["items"]
    assert hist[0]["query"] == "headphone" and hist[0]["times"] >= 3
    client.delete("/api/favorites", params={"key": hit["key"]}, headers=H)
    assert client.get("/api/favorites").json()["items"] == []


def test_presets(client):
    items = client.get("/api/presets").json()["items"]
    assert any(p["name"] == "Проверенные" and p["builtin"] for p in items)
    client.post("/api/presets", json={"name": "Мой", "filters": {"min_likes": 7}}, headers=H)
    mine = next(p for p in client.get("/api/presets").json()["items"] if p["name"] == "Мой")
    assert mine["filters"] == {"min_likes": 7} and not mine["builtin"]
    client.delete(f"/api/presets/{mine['id']}", headers=H)
    assert not any(p["name"] == "Мой" for p in client.get("/api/presets").json()["items"])


def test_subscription_flow(monkeypatch):
    from . import fixtures

    with make(monkeypatch) as c:
        r = c.post("/api/subs", json={"query": "headphone", "interval_min": 60,
                                      "params": {"filters": {"price": "free"}}}, headers=H).json()
        sid = r["id"]
        assert r["run"]["ok"] and r["run"]["first_run"] and r["run"]["new"] == 0
        assert r["sub"]["total"] > 0 and r["sub"]["interval_min"] == 60

        fixtures.PRINTABLES["data"]["searchPrints2"]["items"].append(
            {"id": "999", "name": "Headphone hanger NEW", "slug": "new", "likesCount": 1, "premium": False})
        try:
            c.post("/api/cache/clear", headers=H)
            run = c.post(f"/api/subs/{sid}/run", headers=H).json()["run"]
            assert run["new"] == 1, c.get(f"/api/subs/{sid}/items", params={"new": "1"}).json()["items"]
            items = c.get(f"/api/subs/{sid}/items", params={"new": "1"}).json()["items"]
            assert [i["title"] for i in items] == ["Headphone hanger NEW"]
            assert c.get("/api/subs").json()["new_total"] == 1
            c.post(f"/api/subs/{sid}/seen", headers=H)
            assert c.get("/api/subs").json()["new_total"] == 0
        finally:
            fixtures.PRINTABLES["data"]["searchPrints2"]["items"].pop()

        c.patch(f"/api/subs/{sid}", json={"interval_min": 1440, "enabled": False}, headers=H)
        sub = c.get(f"/api/subs/{sid}").json()["sub"]
        assert sub["interval_min"] == 1440 and sub["enabled"] is False
        c.delete(f"/api/subs/{sid}", headers=H)
        assert c.get("/api/subs").json()["items"] == []


def test_sources_and_config_endpoints(client):
    srcs = client.get("/api/sources").json()["sources"]
    cults = next(s for s in srcs if s["name"] == "cults3d")
    assert cults["configured"] is False and cults["missing"] == ["TEST_CULTS_USER", "TEST_CULTS_KEY"]
    assert next(s for s in srcs if s["name"] == "yeggi")["link_only"] is True
    cfg = client.get("/api/config").json()
    assert set(cfg["weights"]) == {"relevance", "semantic", "source_rank", "popularity", "freshness", "personal"}
    assert cfg["features"]["semantic"]["enabled"] is False
    assert client.get("/").status_code == 200
