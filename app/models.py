from __future__ import annotations

import math
import re
from dataclasses import asdict, dataclass, field, fields, replace
from datetime import datetime, timezone
from typing import Any


@dataclass
class ModelHit:
    source: str
    id: str
    title: str
    url: str
    thumbnail: str | None = None
    author: str | None = None
    likes: int | None = None
    downloads: int | None = None
    views: int | None = None
    makes: int | None = None
    published: datetime | None = None
    price: float | None = None
    currency: str | None = None
    license: str | None = None
    commercial: bool | None = None
    tags: list[str] = field(default_factory=list)
    position: int = 0

    score: float = 0.0
    breakdown: dict[str, float] = field(default_factory=dict)
    also_on: list[dict[str, str]] = field(default_factory=list)

    @property
    def key(self) -> str:
        return f"{self.source}:{self.id}"

    @property
    def is_free(self) -> bool | None:
        if self.price is None:
            return None
        return self.price <= 0

    def copy(self) -> "ModelHit":
        d = {f.name: getattr(self, f.name) for f in fields(self)}
        d["tags"] = list(self.tags)
        d["breakdown"] = dict(self.breakdown)
        d["also_on"] = list(self.also_on)
        return ModelHit(**d)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["published"] = self.published.isoformat() if self.published else None
        d["is_free"] = self.is_free
        d["key"] = self.key
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ModelHit":
        names = {f.name for f in fields(cls)}
        data = {k: v for k, v in d.items() if k in names}
        data["published"] = parse_date(data.get("published"))
        return cls(**data)


_NONCOMMERCIAL = re.compile(
    r"\bnc\b|non[- ]?commercial|\b(?:personal|private) use|\b(?:no|not) commercial|not for (?:commercial|resale|sale)"
    r"|(?:without|prohibits?|forbids?) commercial|commercial (?:use )?(?:is )?(?:not|prohibited|forbidden|disallowed)")
_COMMERCIAL = re.compile(
    r"\bcc0\b|\bcc[- ]?zero\b|public domain|\bcc[- ]?by\b|creative commons\W+(?:\d\S*\W+)?attribution"
    r"|\bmit\b|\b[al]?gpl\b|\bbsd\b|\bapache\b|commercial use (?:is )?(?:allowed|permitted|ok)"
    r"|^commercial(?: licen[cs]e)?$")


def license_allows_commercial(name: str | None) -> bool | None:
    if not name:
        return None
    n = re.sub(r"[\s_]+", " ", name.lower()).strip()
    if _NONCOMMERCIAL.search(n):
        return False
    if _COMMERCIAL.search(n):
        return True
    return None


def parse_date(value: Any) -> datetime | None:
    if value in (None, "", 0):
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        if isinstance(value, (int, float)):
            ts = float(value)
            if ts > 1e12:
                ts /= 1000
            return datetime.fromtimestamp(ts, tz=timezone.utc)
        s = str(value).strip()
        if s.isdigit():
            return parse_date(int(s))
        s = s.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (ValueError, OverflowError, OSError):
        return None


def to_int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError, OverflowError):
        return None


MAX_URL = 2048
MAX_DATA_IMAGE = 300_000
MAX_COUNT = 10**12
_INVISIBLE = re.compile(r"[\x00-\x1f\x7f\u200b-\u200f\u202a-\u202e\u2066-\u2069\ufeff]+")
_WEB_URL = re.compile(r"""^https?://[^\s<>"'`\\]+$""", re.I)
_DATA_IMAGE = re.compile(r"^data:image/(?:png|jpe?g|gif|webp|avif);base64,[A-Za-z0-9+/=]+$")


def _text(value: Any, limit: int) -> str | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        value = str(value)
    if not isinstance(value, str):
        return None
    value = re.sub(r"\s+", " ", _INVISIBLE.sub(" ", value[: limit * 4])).strip()
    return value[:limit] or None


def web_url(value: Any) -> str | None:
    if not isinstance(value, str) or len(value) > MAX_URL:
        return None
    value = value.strip()
    return value if _WEB_URL.match(value) else None


def _count(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return int(value) if 0 <= value <= MAX_COUNT else None


def _money(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) and 0 <= value <= 10_000_000 else None


def _thumbnail(value: Any) -> str | None:
    if isinstance(value, str) and value.startswith("data:"):
        return value if len(value) <= MAX_DATA_IMAGE and _DATA_IMAGE.match(value) else None
    return web_url(value)


def clean_hit(h: ModelHit) -> ModelHit | None:
    """Bring a hit from an untrusted site to sane types and sizes; None when it has no usable link."""
    url = web_url(h.url)
    if not url:
        return None
    tags = h.tags if isinstance(h.tags, list) else []
    tags = [t for t in (_text(x, 60) for x in tags[:200]) if t][:40]
    return replace(
        h,
        id=_text(h.id, 200) or url[:200],
        title=_text(h.title, 300) or "(без названия)",
        url=url,
        thumbnail=_thumbnail(h.thumbnail),
        author=_text(h.author, 120),
        likes=_count(h.likes), downloads=_count(h.downloads), views=_count(h.views), makes=_count(h.makes),
        published=h.published if isinstance(h.published, datetime) else None,
        price=_money(h.price),
        currency=_text(h.currency, 8),
        license=_text(h.license, 120),
        commercial=h.commercial if isinstance(h.commercial, bool) else None,
        tags=tags,
        position=h.position if isinstance(h.position, int) and 0 <= h.position < 10_000 else 0,
    )


def first(d: dict, *keys: str, default: Any = None) -> Any:
    for k in keys:
        cur: Any = d
        for part in k.split("."):
            cur = cur.get(part) if isinstance(cur, dict) else None
            if cur is None:
                break
        if cur not in (None, ""):
            return cur
    return default
