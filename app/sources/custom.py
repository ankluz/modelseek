from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import quote, quote_plus

from ..models import ModelHit, first, license_allows_commercial, parse_date, to_int
from .base import Source, SourceError

PLACEHOLDER = re.compile(r"\{(\w+)\}")


def _fill_url(tpl: str, vals: dict[str, Any]) -> str:
    return PLACEHOLDER.sub(lambda m: quote(str(vals[m.group(1)]), safe="") if m.group(1) in vals else m.group(0), tpl)


def _fill_body(obj: Any, vals: dict[str, Any]) -> Any:
    if isinstance(obj, dict):
        return {k: _fill_body(v, vals) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_fill_body(v, vals) for v in obj]
    if isinstance(obj, str):
        m = PLACEHOLDER.fullmatch(obj)
        if m and m.group(1) in vals:
            return vals[m.group(1)]
        return PLACEHOLDER.sub(lambda mm: str(vals.get(mm.group(1), mm.group(0))), obj)
    return obj


def _absolute(url: Any, prefix: str) -> str | None:
    if not url or not isinstance(url, str):
        return None
    if url.startswith(("http://", "https://", "data:")):
        return url
    if url.startswith("//"):
        return "https:" + url
    return prefix.rstrip("/") + "/" + url.lstrip("/") if prefix else url


class CustomSource(Source):
    official_api = False

    def __init__(self, name: str, cfg: dict[str, Any]):
        self._name = name
        super().__init__(cfg)
        self._label = cfg.get("label") or name
        self._color = cfg.get("color") or "#8a8f98"
        self._homepage = cfg.get("homepage") or ""
        self._search_page = cfg.get("search_page") or ""
        if not self._homepage:
            for u in (cfg.get("url"), self._search_page):
                m = re.match(r"^(https?://[^/]+)", str(u or ""))
                if m:
                    self._homepage = m.group(1)
                    break

    @property
    def name(self) -> str:
        return self._name

    @property
    def label(self) -> str:
        return self._label

    @property
    def color(self) -> str:
        return self._color

    @property
    def homepage(self) -> str:
        return self._homepage

    @property
    def search_page(self) -> str:
        return self._search_page

    def info(self) -> dict[str, Any]:
        d = super().info()
        d["custom"] = True
        return d

    def search_link(self, query: str) -> str | None:
        return self._search_page.format(q=quote_plus(query)) if "{q}" in self._search_page else None

    async def search(self, fetcher, query, limit, page):
        vals = {"q": query, "limit": limit, "page": page, "page0": page - 1, "offset": (page - 1) * limit}
        url = _fill_url(str(self.cfg.get("url", "")), vals)
        headers = {str(k): str(v) for k, v in (self.cfg.get("headers") or {}).items()}
        if str(self.cfg.get("method", "GET")).upper() == "POST":
            raw = self.cfg.get("body") or "{}"
            try:
                tpl = json.loads(raw) if isinstance(raw, str) else raw
            except ValueError as e:
                raise SourceError(f"body: некорректный JSON ({e})") from e
            data = self.check(await fetcher.post_json(url, _fill_body(tpl, vals), headers=headers))
        else:
            data = self.check(await fetcher.get(url, headers=headers))
        items = self.items(data)
        return [self.parse(it, i) for i, it in enumerate(items[:limit]) if isinstance(it, dict)]

    def items(self, data: Any) -> list[Any]:
        path = str(self.cfg.get("items_path") or "").strip()
        items = first(data, path) if path else data
        if not isinstance(items, list):
            raise SourceError(f"по пути «{path or '(корень)'}» нет массива — проверьте items_path")
        return items

    def parse(self, it: dict[str, Any], pos: int) -> ModelHit:
        f = self.cfg.get("fields") or {}
        prefix = str(self.cfg.get("url_prefix") or self._homepage)

        def get(key: str, *defaults: str):
            path = f.get(key)
            return first(it, path) if path else first(it, *defaults) if defaults else None

        mid = get("id", "id", "uid", "_id", "uuid", "identifier")
        url_t = f.get("url") or "url"
        if "{" in url_t:
            url = PLACEHOLDER.sub(lambda m: str(first(it, m.group(1), default="")), url_t)
        else:
            url = first(it, url_t, "link", "href")
        price = get("price")
        tags = get("tags") or []
        if isinstance(tags, str):
            tags = [t.strip() for t in tags.split(",") if t.strip()]
        tags = [t if isinstance(t, str) else first(t, "name", "title", default="") for t in tags]
        lic = get("license")
        lic = lic if isinstance(lic, str) else None
        abs_url = _absolute(url, prefix) or self._homepage
        return ModelHit(
            source=self._name,
            id=str(mid if mid is not None else (abs_url if url else pos)),
            title=str(get("title", "title", "name") or "(без названия)"),
            url=abs_url,
            thumbnail=_absolute(get("thumbnail", "thumbnail", "image", "cover"), str(self.cfg.get("thumb_prefix") or prefix)),
            author=(lambda a: str(a) if a is not None else None)(get("author", "author", "user.name")),
            likes=to_int(get("likes")),
            downloads=to_int(get("downloads")),
            makes=to_int(get("makes")),
            published=parse_date(get("published")),
            price=float(price) if isinstance(price, (int, float)) else None,
            license=lic,
            commercial=license_allows_commercial(lic),
            tags=[t for t in tags if t],
            position=pos,
        )



def find_arrays(data: Any, path: str = "", depth: int = 0, out: list | None = None) -> list[dict[str, Any]]:
    out = [] if out is None else out
    if depth > 5:
        return out
    if isinstance(data, list):
        if data and isinstance(data[0], dict):
            out.append({"path": path, "count": len(data)})
    elif isinstance(data, dict):
        for k, v in data.items():
            find_arrays(v, f"{path}.{k}" if path else str(k), depth + 1, out)
    return out


def flatten_keys(obj: Any, prefix: str = "", depth: int = 0, out: dict | None = None) -> dict[str, Any]:
    out = {} if out is None else out
    if depth > 3 or len(out) > 80:
        return out
    if isinstance(obj, dict):
        for k, v in obj.items():
            flatten_keys(v, f"{prefix}.{k}" if prefix else str(k), depth + 1, out)
    elif isinstance(obj, list):
        out[prefix] = f"[{len(obj)}] " + (json.dumps(obj[0], ensure_ascii=False)[:60] if obj else "")
    else:
        s = json.dumps(obj, ensure_ascii=False) if not isinstance(obj, str) else obj
        out[prefix] = s[:80]
    return out


GUESSES = {
    "id": ("id", "uid", "_id", "uuid", "identifier", "key"),
    "title": ("title", "name", "modelTitle"),
    "url": ("url", "public_url", "link", "href", "permalink"),
    "thumbnail": ("thumbnail", "thumbnailUrl", "thumb", "preview_image", "preview", "image", "image.url", "img", "pic",
                  "picture", "photo", "cover", "coverUrl", "images.0.url"),
    "author": ("author", "author.name", "user.name", "user.username", "user.nick", "creator.name", "creator.nick",
               "owner.username", "owner.name", "owner.nick", "designer.name"),
    "likes": ("likes", "likeCount", "like_count", "likesCount", "favorites", "hearts", "stats.likes", "stats.hearts"),
    "downloads": ("downloads", "downloadCount", "download_count", "downloadsCount", "stats.downloads", "stats.dl"),
    "makes": ("makes", "makesCount", "make_count", "printCount"),
    "published": ("published", "published_at", "publishedAt", "created_at", "createdAt", "createTime", "date"),
    "price": ("price", "price.value", "price.amount"),
    "tags": ("tags", "keywords"),
}


def guess_fields(item: dict[str, Any]) -> dict[str, str]:
    out = {}
    for field, cands in GUESSES.items():
        for c in cands:
            if first(item, c) is not None:
                out[field] = c
                break
    return out
