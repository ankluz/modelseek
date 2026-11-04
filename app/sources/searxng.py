from __future__ import annotations

from urllib.parse import urlparse

from ..models import ModelHit, first, parse_date
from .base import Source


class SearXNG(Source):
    name = "searxng"
    label = "Веб (SearXNG)"
    homepage = "https://docs.searxng.org"
    color = "#3050ff"
    allowed_modes = ("http",)

    @property
    def sites(self) -> list[str]:
        return [s for s in self.cfg.get("sites") or [] if s]

    async def search(self, fetcher, query, limit, page):
        base = str(self.cfg.get("url", "http://127.0.0.1:8089")).rstrip("/")
        site_filter = " OR ".join(f"site:{s}" for s in self.sites)
        q = f"{query} ({site_filter})" if site_filter else query
        data = self.check(await fetcher.get(f"{base}/search", params={"q": q, "format": "json", "pageno": page}))
        rows = data.get("results", [])[:limit]
        return [self.parse(r, i) for i, r in enumerate(rows) if r.get("url")]

    @staticmethod
    def parse(r: dict, pos: int) -> ModelHit:
        url = r["url"]
        host = urlparse(url).hostname or ""
        return ModelHit(
            source="searxng",
            id=url,
            title=(r.get("title") or url).strip(),
            url=url,
            thumbnail=first(r, "thumbnail", "img_src"),
            author=host.removeprefix("www."),
            published=parse_date(r.get("publishedDate")),
            position=pos,
        )
