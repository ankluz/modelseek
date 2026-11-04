from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field, fields
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
    except (TypeError, ValueError):
        return None


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
