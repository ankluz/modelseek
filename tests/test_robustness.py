import json
from copy import deepcopy
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from starlette.testclient import TestClient

from app.background import MAX_LOG_BYTES, open_log
from app.main import create_app
from app.storage import Storage

from .fixtures import PRINTABLES, router
from .test_api import CFG, H


class Sites:
    def __init__(self):
        self.fail: set[str] = set()
        self.inner = router()
        self.orderings: list = []
        self.printables = None
        self.translation = None

    def __call__(self, req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/chat/completions"):
            return httpx.Response(200, json={"choices": [{"message": {"content": self.translation}}]})
        if req.url.host in self.fail:
            return httpx.Response(500, text="boom")
        if req.url.host == "api.printables.com":
            self.orderings.append(json.loads(req.content)["variables"]["ordering"])
            if self.printables is not None:
                return httpx.Response(200, json=self.printables)
        return self.inner.handler(req)


@pytest.fixture
def sites():
    return Sites()


def app(sites, tmp_path, cfg=None):
    return TestClient(create_app(deepcopy(cfg or CFG), transport=httpx.MockTransport(sites), db_path=":memory:",
                                 start_scheduler=False, data_dir=str(tmp_path)), raise_server_exceptions=False)


@pytest.fixture
def client(sites, tmp_path):
    with app(sites, tmp_path) as c:
        yield c


@pytest.mark.parametrize("qs", ["min_likes=abc", "max_price=abc", "days=abc", "min_makes=inf", "min_downloads=nan"])
def test_bad_filter_values_are_client_errors(client, qs):
    assert client.get(f"/api/search?q=x&{qs}").status_code == 400
    assert client.get(f"/api/search/stream?q=x&{qs}").status_code == 400


@pytest.mark.parametrize("qs", ["page=nan", "page=inf", "w_relevance=nan", "sw_printables=inf", "half_life=nan"])
def test_non_finite_numbers_fall_back_to_defaults(client, qs):
    r = client.get(f"/api/search?q=headphone&{qs}")
    assert r.status_code == 200 and r.json()["results"]


@pytest.mark.parametrize("payload", [{"interval_min": "abc"}, {"interval_min": 10 ** 30}, {"params": {"filters": "bad"}},
                                     {"params": {"weights": 1}}, {"params": {"sources": "x"}},
                                     {"params": {"filters": {"min_likes": "abc"}}}])
def test_bad_subscription_is_rejected_and_not_saved(client, payload):
    r = client.post("/api/subs", json={"query": "x", **payload}, headers=H)
    assert r.status_code == 400
    assert client.get("/api/subs").json()["items"] == []


def test_bad_interval_on_update(client):
    sid = client.post("/api/subs", json={"query": "headphone"}, headers=H).json()["id"]
    assert client.patch(f"/api/subs/{sid}", json={"interval_min": "abc"}, headers=H).status_code == 400
    assert client.patch(f"/api/subs/{sid}", json={"interval_min": 120}, headers=H).json()["sub"]["interval_min"] == 120


@pytest.mark.parametrize("content", ["", "   ", None, 5])
def test_empty_translation_does_not_break_search(sites, tmp_path, content):
    cfg = deepcopy(CFG)
    cfg["translate"]["enabled"] = True
    sites.translation = content
    with app(sites, tmp_path, cfg) as c:
        r = c.get("/api/search", params={"q": "держатель"})
    assert r.status_code == 200 and r.json()["translated"] is None


def test_good_translation_is_used(sites, tmp_path):
    cfg = deepcopy(CFG)
    cfg["translate"]["enabled"] = True
    sites.translation = '"headphone holder"\nextra line'
    with app(sites, tmp_path, cfg) as c:
        assert c.get("/api/search", params={"q": "держатель"}).json()["translated"] == "headphone holder"


@pytest.mark.parametrize("tpl", ["https://x.io/{q}/{foo}", "https://x.io/{q}/{}", "https://x.io/{q}/{"])
def test_stray_braces_in_links_do_not_break_search(sites, tmp_path, tpl):
    cfg = deepcopy(CFG)
    cfg["links"]["bad"] = {"label": "Bad", "url": tpl}
    cfg["custom_sources"] = {"own": {"mode": "link", "search_page": tpl}}
    with app(sites, tmp_path, cfg) as c:
        r = c.get("/api/search", params={"q": "lamp shade"})
        assert r.status_code == 200
        st = {s["name"]: s for s in r.json()["sources"]}
        assert st["bad"]["link"] == tpl.replace("{q}", "lamp+shade") and st["printables"]["status"] == "ok"
        assert c.get("/api/sources").status_code == 200


def test_incomplete_favorite_is_rejected(client):
    for hit in ({"source": "x", "id": "1"},
                {"source": "x", "id": "1", "title": "t"},
                {"source": "x", "id": "1", "title": "t", "url": "javascript:alert(1)"},
                {"source": "x", "id": "1", "title": "t", "url": "https://a", "tags": 5}):
        assert client.post("/api/favorites", json={"hit": hit}, headers=H).status_code == 400
    assert client.get("/api/favorites").json()["items"] == []


def test_broken_favorite_already_in_db_does_not_break_search(tmp_path):
    st = Storage(":memory:")
    st.add_favorite({"source": "x", "id": "1"})
    st.add_favorite({"source": "x", "id": "2", "title": 5, "url": "u"})
    st.add_favorite({"source": "x", "id": "3", "title": "Good", "url": "https://a", "tags": ["ok", 1]})
    st.add_favorite({"source": "x", "id": "4", "title": "No tags", "url": "https://a", "tags": 7})
    hits = {h.id: h for h in st.favorite_hits()}
    assert set(hits) == {"3", "4"} and hits["3"].tags == ["ok"] and hits["4"].tags == []
    st.close()


def test_hits_with_unsafe_links_are_dropped(sites, client):
    sites.printables = {"data": {"searchPrints2": {"items": [{"id": "1", "name": "Headphone A", "slug": "a"}]}}}
    res = client.get("/api/search", params={"q": "headphone", "sources": "thangs"}).json()["results"]
    assert res
    from app.engine import Engine
    from app.models import ModelHit
    from app.sources.base import Source

    class Bad(Source):
        name = label = "bad"

        async def search(self, fetcher, query, limit, page):
            return [ModelHit("bad", "1", "ok", "https://a"), ModelHit("bad", "2", "evil", "javascript:alert(1)")]

    import asyncio

    async def go():
        cfg = deepcopy(CFG)
        async with httpx.AsyncClient(transport=httpx.MockTransport(sites)) as http:
            engine = Engine({"bad": Bad({})}, http, cfg)
            hits, _ = await engine.search("x", ["bad"])
            await engine.aclose()
            return hits

    assert [h.id for h in asyncio.run(go())] == ["1"]


def latest_item(i, name, days=1):
    published = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    return {"id": str(i), "name": name, "slug": "m", "likesCount": 0, "premium": False, "datePublished": published}


def test_default_search_asks_site_for_best_match(sites, client):
    client.get("/api/search", params={"q": "headphone", "sources": "printables"})
    assert sites.orderings == ["best_match"]


@pytest.mark.parametrize("params,ordering", [({"sort": "newest"}, "latest"), ({"days": 30}, "latest"),
                                             ({"sort": "likes"}, "popular"), ({"sort": "makes"}, "makes_count"),
                                             ({"sort": "likes", "days": 30}, "latest")])
def test_sort_and_date_filter_use_site_ordering(sites, client, params, ordering):
    client.get("/api/search", params={"q": "headphone", "sources": "printables", **params})
    assert sites.orderings == [ordering]


def test_orderings_do_not_share_cache(sites, client):
    q = {"q": "headphone", "sources": "printables"}
    client.get("/api/search", params=q)
    client.get("/api/search", params={**q, "sort": "newest"})
    client.get("/api/search", params={**q, "sort": "newest"})
    assert sites.orderings == ["best_match", "latest"]


def test_date_filter_finds_fresh_models_and_drops_off_topic(sites, client):
    sites.printables = {"data": {"searchPrints2": {"items": [
        latest_item(1, "Benchy fresh"), latest_item(2, "Quick-Release Bench Vise"), latest_item(3, "Old benchy", days=90)]}}}
    res = client.get("/api/search", params={"q": "benchy", "sources": "printables", "days": 30}).json()["results"]
    assert [h["title"] for h in res] == ["Benchy fresh"]


def test_sources_without_native_sort_keep_loose_matches(client):
    res = client.get("/api/search", params={"q": "zzz", "sources": "makerworld", "sort": "newest"}).json()["results"]
    assert res


def create_sub(c, query="headphone"):
    return c.post("/api/subs", json={"query": query, "interval_min": 60, "params": {"sources": ["printables", "makerworld", "thangs"]}},
                  headers=H).json()


def rerun(c, sid):
    c.post("/api/cache/clear", headers=H)
    return c.post(f"/api/subs/{sid}/run", headers=H).json()["run"]


def test_sub_after_total_failure_does_not_report_everything_as_new(sites, client):
    sites.fail = {"api.printables.com", "makerworld.com", "thangs.com"}
    r = create_sub(client)
    assert r["run"]["first_run"] and r["run"]["total"] == 0 and r["run"]["errors"]
    sites.fail = set()
    run = rerun(client, r["id"])
    assert run["new"] == 0 and run["total"] > 0 and run["first_run"] is True
    assert rerun(client, r["id"])["first_run"] is False


def test_sub_source_that_returns_is_not_reported_as_new(sites, client):
    sites.fail = {"makerworld.com"}
    r = create_sub(client)
    assert r["run"]["total"] > 0
    sites.fail = set()
    assert rerun(client, r["id"])["new"] == 0


def test_sub_reports_model_that_appears_during_recovery(sites, client):
    r = create_sub(client)
    sites.fail = {"makerworld.com"}
    assert rerun(client, r["id"])["new"] == 0
    sites.fail = set()
    PRINTABLES["data"]["searchPrints2"]["items"].append(latest_item(999, "Headphone hanger NEW"))
    try:
        run = rerun(client, r["id"])
        assert run["new"] == 1
        new = client.get(f"/api/subs/{r['id']}/items", params={"new": "1"}).json()["items"]
        assert [i["title"] for i in new] == ["Headphone hanger NEW"]
    finally:
        PRINTABLES["data"]["searchPrints2"]["items"].pop()


def test_sub_is_checked_by_newest_order(sites, client):
    r = create_sub(client)
    assert sites.orderings == ["latest"]
    assert client.get(f"/api/subs/{r['id']}").json()["sub"]["params"]["sort"] == "score"


def test_new_model_on_known_source_counts_even_if_merged_with_new_source():
    st = Storage(":memory:")
    sid = st.add_sub("x", {}, 60)
    assert st.record_sub_items(sid, [{"key": "a:1", "title": "t"}], ["a"]) == 0
    hits = [{"key": "b:9", "title": "new", "also_on": [{"key": "a:2"}]}, {"key": "b:8", "title": "old on new source"}]
    assert st.record_sub_items(sid, hits, ["a", "b"]) == 1
    assert [i["key"] for i in st.sub_items(sid, only_new=True)] == ["b:9"]
    st.delete_sub(sid)
    assert st.sub_baseline(sid) == set()
    st.close()


def test_background_log_rotates(tmp_path):
    log = tmp_path / "data" / "modelseek.log"
    with open_log(tmp_path / "data") as f:
        f.write("first\n")
    assert log.read_text(encoding="utf-8") == "first\n"
    log.write_bytes(b"x" * (MAX_LOG_BYTES + 1))
    with open_log(tmp_path / "data") as f:
        f.write("second\n")
    assert log.read_text(encoding="utf-8") == "second\n"
    assert (tmp_path / "data" / "modelseek.log.1").stat().st_size > MAX_LOG_BYTES
