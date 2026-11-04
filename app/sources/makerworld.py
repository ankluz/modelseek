from __future__ import annotations

from ..models import ModelHit, first, license_allows_commercial, parse_date, to_int
from .base import Source

API = "https://makerworld.com/api/v1/search-service/select/design2"


class MakerWorld(Source):
    name = "makerworld"
    label = "MakerWorld"
    homepage = "https://makerworld.com"
    search_page = "https://makerworld.com/en/search/models?keyword={q}"
    color = "#00ae42"
    default_mode = "link"

    async def search(self, fetcher, query, limit, page):
        params = {"keyword": query, "limit": limit, "offset": (page - 1) * limit, "orderBy": "score"}
        headers = {"Accept": "application/json"}
        if fetcher.mode != "browser":
            headers["Referer"] = "https://makerworld.com/en/search/models"
        data = self.check(await fetcher.get(API, params=params, headers=headers))
        hits = first(data, "hits", "data.hits", "list", default=[]) or []
        return [self.parse(h, i) for i, h in enumerate(hits) if h]

    @staticmethod
    def parse(h: dict, pos: int) -> ModelHit:
        mid = str(first(h, "id", "designId"))
        slug = h.get("slug")
        lic = first(h, "license", "licenseName")
        lic = lic if isinstance(lic, str) else None
        return ModelHit(
            source="makerworld",
            id=mid,
            title=first(h, "title", "titleTranslated", default="(без названия)"),
            url=f"https://makerworld.com/en/models/{mid}" + (f"-{slug}" if slug else ""),
            thumbnail=first(h, "cover", "coverUrl"),
            author=first(h, "designCreator.name", "designCreator.handle", "creatorName"),
            likes=to_int(first(h, "likeCount", "likes")),
            downloads=to_int(first(h, "downloadCount", "downloads")),
            makes=to_int(first(h, "printCount")),
            published=parse_date(first(h, "createTime", "publishTime")),
            price=0.0 if not h.get("isExclusive") and not h.get("price") else None,
            license=lic,
            commercial=license_allows_commercial(lic),
            tags=[t for t in h.get("tags") or [] if isinstance(t, str)],
            position=pos,
        )
