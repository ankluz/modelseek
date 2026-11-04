from __future__ import annotations

from ..models import ModelHit, first, license_allows_commercial, parse_date, to_int
from .base import Source, SourceError

API = "https://api.printables.com/graphql/"
MEDIA = "https://media.printables.com/"
THUMB_SIZE = "640x480"

BASE_FIELDS = """
      id name slug likesCount downloadCount datePublished premium price
      user { publicUsername handle }
      image { filePath }
      tags { name }
"""
EXTRA_FIELDS = """
      makesCount
      license { name abbreviation }
"""

QUERY = """
query SearchModels($query: String!, $limit: Int, $offset: Int, $ordering: SearchChoicesEnum) {
  searchPrints2(query: $query, limit: $limit, offset: $offset, ordering: $ordering) {
    totalCount
    items { %s }
  }
}
"""


def thumbnail_url(path: str | None) -> str | None:
    if not path or str(path).startswith(("http", "data:")):
        return path
    folder, _, name = str(path).lstrip("/").rpartition("/")
    stem, dot, ext = name.rpartition(".")
    if not folder or not dot:
        return MEDIA + str(path).lstrip("/")
    return f"{MEDIA}{folder}/thumbs/inside/{THUMB_SIZE}/{ext.lower()}/{stem}.webp"


class Printables(Source):
    name = "printables"
    label = "Printables"
    homepage = "https://www.printables.com"
    search_page = "https://www.printables.com/search/models?q={q}"
    color = "#fa6831"
    native_sorts = {"newest": "latest", "likes": "popular", "downloads": "popular", "makes": "makes_count"}

    _extended_ok = True

    async def search(self, fetcher, query, limit, page, sort=None):
        variables = {"query": query, "limit": limit, "offset": (page - 1) * limit, "ordering": sort or "best_match"}
        if Printables._extended_ok:
            data = await self._run(fetcher, BASE_FIELDS + EXTRA_FIELDS, variables)
            if data.get("errors") and not first(data, "data.searchPrints2"):
                Printables._extended_ok = False
                data = await self._run(fetcher, BASE_FIELDS, variables)
        else:
            data = await self._run(fetcher, BASE_FIELDS, variables)
        if data.get("errors") and not first(data, "data.searchPrints2"):
            raise SourceError("GraphQL: " + str(data["errors"][0].get("message", "ошибка"))[:120])
        items = first(data, "data.searchPrints2.items", default=[]) or []
        return [self.parse(it, i) for i, it in enumerate(items) if it]

    async def _run(self, fetcher, fields, variables):
        payload = {"operationName": "SearchModels", "query": QUERY % fields, "variables": variables}
        headers = {"Origin": "https://www.printables.com", "Referer": "https://www.printables.com/"}
        return self.check(await fetcher.post_json(API, payload, headers=headers))

    @staticmethod
    def parse(it: dict, pos: int) -> ModelHit:
        mid = str(it.get("id"))
        slug = it.get("slug") or ""
        img = first(it, "image.filePath")
        premium = bool(it.get("premium"))
        price = it.get("price")
        lic = first(it, "license.abbreviation", "license.name")
        return ModelHit(
            source="printables",
            id=mid,
            title=it.get("name") or "(без названия)",
            url=f"https://www.printables.com/model/{mid}-{slug}".rstrip("-"),
            thumbnail=thumbnail_url(img),
            author=first(it, "user.publicUsername", "user.handle"),
            likes=to_int(it.get("likesCount")),
            downloads=to_int(it.get("downloadCount")),
            makes=to_int(it.get("makesCount")),
            published=parse_date(it.get("datePublished")),
            price=(float(price) if price else None) if premium else 0.0,
            currency="EUR" if premium else None,
            license=lic,
            commercial=license_allows_commercial(lic),
            tags=[t.get("name") for t in it.get("tags") or [] if isinstance(t, dict) and t.get("name")],
            position=pos,
        )
