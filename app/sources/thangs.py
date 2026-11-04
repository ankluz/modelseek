from __future__ import annotations

from ..models import ModelHit, first, parse_date, to_int
from .base import Source

API = "https://thangs.com/api/models/v2/search-by-text"


class Thangs(Source):
    name = "thangs"
    label = "Thangs"
    homepage = "https://thangs.com"
    search_page = "https://thangs.com/search/{q}?scope=all"
    color = "#7b61ff"
    default_mode = "link"

    async def search(self, fetcher, query, limit, page):
        params = {"searchTerm": query, "page": page - 1, "pageSize": limit, "scope": "all"}
        data = self.check(await fetcher.get(API, params=params, headers={"Accept": "application/json"}))
        rows = first(data, "results", "data.results", "models", default=[]) or []
        return [self.parse(r, i) for i, r in enumerate(rows) if r]

    @staticmethod
    def parse(r: dict, pos: int) -> ModelHit:
        mid = str(first(r, "modelId", "id", default=pos))
        url = first(r, "attributionUrl", "modelUrl", "url")
        if not url:
            url = f"https://thangs.com/m/{mid}"
        elif url.startswith("/"):
            url = "https://thangs.com" + url
        return ModelHit(
            source="thangs",
            id=mid,
            title=first(r, "modelTitle", "name", "title", default="(без названия)"),
            url=url,
            thumbnail=first(r, "thumbnailUrl", "thumbnail", "previewImage"),
            author=first(r, "ownerUsername", "owner.username", "authorName"),
            likes=to_int(first(r, "likesCount", "likes")),
            downloads=to_int(first(r, "downloadCount", "downloads")),
            published=parse_date(first(r, "createdAt", "uploadDate")),
            price=0.0 if first(r, "isFree", default=None) is True else None,
            position=pos,
        )
