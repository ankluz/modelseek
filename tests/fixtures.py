import json

import httpx

PRINTABLES = {"data": {"searchPrints2": {"totalCount": 2, "items": [
    {"id": "101", "name": "Headphone Holder Desk Clamp", "slug": "headphone-holder", "likesCount": 900,
     "downloadCount": 5000, "datePublished": "2025-06-01T10:00:00Z", "premium": False, "price": None,
     "user": {"publicUsername": "maker_anna"}, "image": {"filePath": "media/prints/101/img.webp"},
     "tags": [{"name": "headphones"}, {"name": "desk"}],
     "makesCount": 12, "license": {"name": "Creative Commons BY", "abbreviation": "CC BY"}},
    {"id": "102", "name": "Cable clip", "slug": "cable-clip", "likesCount": 40, "downloadCount": 200,
     "datePublished": "2021-01-01T00:00:00Z", "premium": True, "price": 3.5,
     "user": {"publicUsername": "bob"}, "image": {"filePath": "media/prints/102/img.webp"}, "tags": []},
]}}}

MAKERWORLD = {"total": 2, "hits": [
    {"id": 555, "title": "Headphone Holder Desk Clamp", "cover": "https://cdn.mw/555.jpg", "likeCount": 30000,
     "downloadCount": 400000, "printCount": 90000, "createTime": "2025-09-01T00:00:00Z",
     "designCreator": {"name": "maker_anna"}},
    {"id": 556, "title": "Headphone stand minimal", "cover": "https://cdn.mw/556.jpg", "likeCount": 12000,
     "downloadCount": 100000, "createTime": 1735689600000, "designCreator": {"name": "lin"}},
]}

THINGIVERSE = {"total": 1, "hits": [
    {"id": 777, "name": "Headphone hook under desk", "public_url": "https://www.thingiverse.com/thing:777",
     "preview_image": "https://cdn.thingiverse.com/777.jpg", "like_count": 1500, "collect_count": 2100,
     "creator": {"name": "oldschool"}, "created_at": "2016-03-03T12:00:00+00:00"},
]}

THANGS = {"results": [
    {"modelId": 42, "modelTitle": "Headphone Holder", "thumbnailUrl": "https://t/42.png", "likesCount": 10,
     "ownerUsername": "x", "attributionUrl": "/designer/x/3d-model/42"},
]}

MMF = {"total_count": 1, "items": [
    {"id": 9, "name": "Dragon headphone stand", "url": "https://www.myminifactory.com/object/9",
     "images": [{"thumbnail": {"url": "https://mmf/9.jpg"}}], "likes": 300, "views": 9000,
     "designer": {"name": "drago"}, "published_at": "2024-02-02T00:00:00Z", "price": {"value": 4.99, "symbol": "€"}},
]}

CULTS = {"data": {"creationsSearchBatch": {"total": 1, "results": [
    {"identifier": "c1", "name": "Headphone holder skull", "url": "https://cults3d.com/en/3d-model/c1",
     "illustrationImageUrl": "https://cults/c1.jpg", "likesCount": 120, "downloadsCount": 800,
     "publishedAt": "2024-05-05T00:00:00Z", "tags": ["skull"], "price": {"cents": 0}, "creator": {"nick": "sk"}},
]}}}


CHALLENGE_HTML = "<html><title>Just a moment...</title><script src='/cdn-cgi/challenge-platform/x'></script></html>"


def router(fail: set[str] | None = None, challenge: set[str] | None = None, calls: list | None = None):
    fail = fail or set()
    challenge = challenge or set()

    def handler(request: httpx.Request) -> httpx.Response:
        host = request.url.host
        if calls is not None:
            calls.append(host)
        if host in challenge:
            return httpx.Response(403, text=CHALLENGE_HTML)
        if host in fail:
            return httpx.Response(403, text="<html>forbidden</html>")
        table = {
            "api.printables.com": PRINTABLES,
            "makerworld.com": MAKERWORLD,
            "api.thingiverse.com": THINGIVERSE,
            "thangs.com": THANGS,
            "www.myminifactory.com": MMF,
            "cults3d.com": CULTS,
        }
        if host in table:
            return httpx.Response(200, content=json.dumps(table[host]), headers={"content-type": "application/json"})
        return httpx.Response(404)

    return httpx.MockTransport(handler)
