from __future__ import annotations

from urllib.parse import quote

from ..models import ModelHit, first, license_allows_commercial, parse_date, to_int
from .base import Source

API = "https://api.thingiverse.com/search/{q}/"


class Thingiverse(Source):
    name = "thingiverse"
    label = "Thingiverse"
    homepage = "https://www.thingiverse.com"
    search_page = "https://www.thingiverse.com/search?q={q}&type=things"
    color = "#248bfb"
    official_api = True
    secret_keys = ("token_env",)
    allowed_modes = ("http", "impersonate", "link")
    native_sorts = {"newest": "newest", "likes": "popular", "downloads": "popular", "makes": "makes"}

    async def search(self, fetcher, query, limit, page, sort=None):
        params = {"type": "things", "per_page": limit, "page": page, "sort": sort or "relevant"}
        headers = {"Authorization": f"Bearer {self.secret('token_env')}"}
        data = self.check(await fetcher.get(API.format(q=quote(query, safe="")), params=params, headers=headers))
        hits = data.get("hits", []) if isinstance(data, dict) else data
        return [self.parse(h, i) for i, h in enumerate(hits or []) if h]

    @staticmethod
    def parse(h: dict, pos: int) -> ModelHit:
        mid = str(h.get("id"))
        lic = h.get("license") if isinstance(h.get("license"), str) else None
        return ModelHit(
            source="thingiverse",
            id=mid,
            title=h.get("name") or "(без названия)",
            url=first(h, "public_url", default=f"https://www.thingiverse.com/thing:{mid}"),
            thumbnail=first(h, "preview_image", "thumbnail"),
            author=first(h, "creator.name", "creator.first_name"),
            likes=to_int(h.get("like_count")),
            downloads=to_int(first(h, "download_count", "collect_count")),
            makes=to_int(h.get("make_count")),
            published=parse_date(first(h, "created_at", "added")),
            price=0.0,
            license=lic,
            commercial=license_allows_commercial(lic),
            position=pos,
        )
