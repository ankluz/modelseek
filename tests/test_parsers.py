import asyncio

import httpx

from app.fetchers import HttpFetcher
from app.sources import Cults3D, MakerWorld, MyMiniFactory, Printables, SearXNG, Thangs, Thingiverse

from .fixtures import router


def run(src, q="headphone"):
    async def go():
        async with httpx.AsyncClient(transport=router()) as c:
            return await src.search(HttpFetcher(c), q, 24, 1)
    return asyncio.run(go())


def test_printables():
    hits = run(Printables({}))
    a, b = hits
    assert a.url == "https://www.printables.com/model/101-headphone-holder"
    assert a.thumbnail == "https://media.printables.com/media/prints/101/img.webp"
    assert a.author == "maker_anna" and a.likes == 900 and a.price == 0.0 and a.tags == ["headphones", "desk"]
    assert a.makes == 12 and a.license == "CC BY" and a.commercial is True
    assert b.price == 3.5 and b.position == 1


def test_printables_falls_back_when_schema_changes():
    import json as _json
    from app.sources import printables as mod
    seen = []

    def handler(req):
        q = _json.loads(req.content)["query"]
        seen.append("makesCount" in q)
        if "makesCount" in q:
            return httpx.Response(200, json={"errors": [{"message": "Cannot query field makesCount"}], "data": None})
        return httpx.Response(200, json={"data": {"searchPrints2": {"items": [
            {"id": "1", "name": "X", "slug": "x", "premium": False}]}}})

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            return await Printables({}).search(HttpFetcher(c), "x", 5, 1)

    mod.Printables._extended_ok = True
    try:
        (h,) = asyncio.run(go())
        assert h.title == "X" and seen == [True, False]
        asyncio.run(go())
        assert seen == [True, False, False]
    finally:
        mod.Printables._extended_ok = True


def test_makerworld():
    a, b = run(MakerWorld({}))
    assert a.url == "https://makerworld.com/en/models/555" and a.downloads == 400000
    assert b.published.year == 2025


def test_thingiverse(monkeypatch):
    monkeypatch.setenv("TV", "x")
    (a,) = run(Thingiverse({"token_env": "TV"}))
    assert a.url.endswith("thing:777") and a.likes == 1500 and a.price == 0.0


def test_thangs_relative_url():
    (a,) = run(Thangs({}))
    assert a.url == "https://thangs.com/designer/x/3d-model/42"


def test_mmf_and_cults(monkeypatch):
    monkeypatch.setenv("K", "x")
    (m,) = run(MyMiniFactory({"key_env": "K"}))
    assert m.thumbnail == "https://mmf/9.jpg" and m.price == 4.99
    (c,) = run(Cults3D({"user_env": "K", "key_env": "K"}))
    assert c.price == 0.0 and c.author == "sk"


def test_searxng_builds_site_filter():
    seen = {}

    def handler(req: httpx.Request):
        seen["q"] = req.url.params["q"]
        return httpx.Response(200, json={"results": [
            {"url": "https://grabcad.com/library/gear-1", "title": "Gear", "img_src": "https://g/1.png"}]})

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            return await SearXNG({"url": "http://s:8080", "sites": ["grabcad.com", "cgtrader.com"]}).search(HttpFetcher(c), "gear", 10, 1)

    (h,) = asyncio.run(go())
    assert seen["q"] == "gear (site:grabcad.com OR site:cgtrader.com)"
    assert h.author == "grabcad.com" and h.thumbnail == "https://g/1.png"


def test_missing_key_is_reported():
    assert Thingiverse({"token_env": "DEFINITELY_NOT_SET"}).missing_secrets == ["DEFINITELY_NOT_SET"]
