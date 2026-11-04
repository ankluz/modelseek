from __future__ import annotations

from ..models import ModelHit, first, license_allows_commercial, parse_date, to_int
from .base import Source, SourceError

API = "https://cults3d.com/graphql"

QUERY = """
query Search($q: String!, $limit: Int, $offset: Int) {
  creationsSearchBatch(query: $q, limit: $limit, offset: $offset) {
    total
    results {
      identifier
      name(locale: EN)
      url(locale: EN)
      illustrationImageUrl
      likesCount
      downloadsCount
      publishedAt
      tags(locale: EN)
      price(currency: EUR) { cents }
      creator { nick }
    }
  }
}
"""


class Cults3D(Source):
    name = "cults3d"
    label = "Cults3D"
    homepage = "https://cults3d.com"
    search_page = "https://cults3d.com/en/search?q={q}"
    color = "#e94e77"
    official_api = True
    secret_keys = ("user_env", "key_env")
    allowed_modes = ("http", "impersonate", "link")

    async def search(self, fetcher, query, limit, page):
        auth = (self.secret("user_env") or "", self.secret("key_env") or "")
        payload = {"query": QUERY, "variables": {"q": query, "limit": limit, "offset": (page - 1) * limit}}
        data = self.check(await fetcher.post_json(API, payload, auth=auth))
        if data.get("errors") and not data.get("data"):
            raise SourceError("GraphQL: " + str(data["errors"][0].get("message", "ошибка"))[:120])
        rows = first(data, "data.creationsSearchBatch.results", default=[]) or []
        return [self.parse(r, i) for i, r in enumerate(rows) if r]

    @staticmethod
    def parse(r: dict, pos: int) -> ModelHit:
        cents = first(r, "price.cents")
        lic = None
        return ModelHit(
            source="cults3d",
            id=str(r.get("identifier")),
            title=r.get("name") or "(без названия)",
            url=r.get("url") or "https://cults3d.com",
            thumbnail=r.get("illustrationImageUrl"),
            author=first(r, "creator.nick"),
            likes=to_int(r.get("likesCount")),
            downloads=to_int(r.get("downloadsCount")),
            published=parse_date(r.get("publishedAt")),
            price=(cents / 100) if isinstance(cents, (int, float)) else None,
            currency="EUR",
            license=lic,
            commercial=license_allows_commercial(lic),
            tags=[t for t in r.get("tags") or [] if isinstance(t, str)],
            position=pos,
        )
