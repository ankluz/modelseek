from __future__ import annotations

from ..models import ModelHit, first, license_allows_commercial, parse_date, to_int
from .base import Source

API = "https://www.myminifactory.com/api/v2/search"


class MyMiniFactory(Source):
    name = "myminifactory"
    label = "MyMiniFactory"
    homepage = "https://www.myminifactory.com"
    search_page = "https://www.myminifactory.com/search?query={q}"
    color = "#f4b400"
    official_api = True
    secret_keys = ("key_env",)
    allowed_modes = ("http", "impersonate", "link")

    async def search(self, fetcher, query, limit, page):
        params = {"q": query, "per_page": limit, "page": page, "key": self.secret("key_env")}
        data = self.check(await fetcher.get(API, params=params))
        rows = first(data, "items", default=[]) or []
        return [self.parse(r, i) for i, r in enumerate(rows) if r]

    @staticmethod
    def parse(r: dict, pos: int) -> ModelHit:
        images = r.get("images") or []
        thumb = None
        if images and isinstance(images[0], dict):
            thumb = first(images[0], "thumbnail.url", "standard.url", "original.url")
        price = first(r, "price.value", "price")
        lic = first(r, "license", "licenses.0.type")
        lic = lic if isinstance(lic, str) else None
        return ModelHit(
            source="myminifactory",
            id=str(r.get("id")),
            title=r.get("name") or "(без названия)",
            url=r.get("url") or f"https://www.myminifactory.com/object/{r.get('id')}",
            thumbnail=thumb,
            author=first(r, "designer.name", "designer.username"),
            likes=to_int(r.get("likes")),
            views=to_int(r.get("views")),
            published=parse_date(r.get("published_at")),
            price=float(price) if isinstance(price, (int, float)) else (0.0 if r.get("free") else None),
            currency=first(r, "price.symbol", default=None),
            license=lic,
            commercial=license_allows_commercial(lic),
            tags=[t for t in r.get("tags") or [] if isinstance(t, str)],
            position=pos,
        )
