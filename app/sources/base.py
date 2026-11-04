from __future__ import annotations

from typing import Any, ClassVar
from urllib.parse import quote_plus

from ..config import env
from ..fetchers import Fetcher, FetchResult
from ..models import ModelHit

MODES = ("http", "impersonate", "browser", "link")


class SourceError(Exception):
    pass


class ChallengeError(SourceError):
    pass


class Source:
    name: ClassVar[str] = ""
    label: ClassVar[str] = ""
    homepage: ClassVar[str] = ""
    color: ClassVar[str] = "#888888"
    official_api: ClassVar[bool] = False
    search_page: ClassVar[str] = ""
    default_mode: ClassVar[str] = "http"
    allowed_modes: ClassVar[tuple[str, ...]] = MODES
    secret_keys: ClassVar[tuple[str, ...]] = ()
    native_sorts: ClassVar[dict[str, str]] = {}

    def __init__(self, cfg: dict[str, Any]):
        self.cfg = cfg or {}
        self.weight: float = float(self.cfg.get("weight", 1.0))
        self.enabled: bool = bool(self.cfg.get("enabled", True))
        mode = str(self.cfg.get("mode", self.default_mode)).lower()
        self.mode = mode if mode in self.allowed_modes else self.default_mode

    def secret(self, key: str) -> str | None:
        return env(self.cfg.get(key))

    @property
    def missing_secrets(self) -> list[str]:
        if self.mode == "link":
            return []
        return [self.cfg.get(k, k) for k in self.secret_keys if not self.secret(k)]

    @property
    def configured(self) -> bool:
        return not self.missing_secrets

    def native_sort(self, order: str | None) -> str | None:
        return self.native_sorts.get(order) if order else None

    def search_link(self, query: str) -> str | None:
        return self.search_page.replace("{q}", quote_plus(query)) if self.search_page else None

    def info(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "label": self.label,
            "homepage": self.homepage,
            "color": self.color,
            "enabled": self.enabled,
            "configured": self.configured,
            "missing": self.missing_secrets,
            "weight": self.weight,
            "official_api": self.official_api,
            "mode": self.mode,
            "link_only": self.mode == "link",
            "search_page": self.search_page or None,
        }

    async def search(self, fetcher: Fetcher, query: str, limit: int, page: int) -> list[ModelHit]:
        raise NotImplementedError

    @staticmethod
    def check(resp: FetchResult) -> Any:
        if resp.looks_like_challenge:
            raise ChallengeError("сайт показал проверку «вы не бот»")
        if resp.status == 409:
            raise SourceError("ждём, пока вы пройдёте проверку в окне браузера")
        if resp.status in (401, 403):
            raise SourceError(f"доступ запрещён (HTTP {resp.status}) — проверьте ключ или включите режим browser")
        if resp.status == 429:
            raise SourceError("слишком много запросов (HTTP 429)")
        if resp.status >= 400:
            raise SourceError(f"HTTP {resp.status}")
        try:
            return resp.json()
        except ValueError as e:
            raise SourceError("ответ не JSON (возможно, страница защиты от ботов)") from e


class LinkSource(Source):

    allowed_modes = ("link",)
    default_mode = "link"

    def __init__(self, name: str, cfg: dict[str, Any]):
        super().__init__({**cfg, "mode": "link"})
        self._name = name
        self._label = cfg.get("label", name)
        self._url = cfg.get("url", "")
        self._color = cfg.get("color", "#888888")

    def info(self) -> dict[str, Any]:
        d = super().info()
        d.update(name=self._name, label=self._label, color=self._color, homepage=self._url.split("?")[0],
                 search_page=self._url)
        return d

    def search_link(self, query: str) -> str | None:
        return self._url.replace("{q}", quote_plus(query)) if self._url else None

    @property
    def name(self) -> str:
        return self._name

    @property
    def label(self) -> str:
        return self._label
