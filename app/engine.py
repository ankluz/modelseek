from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from .fetchers import (BrowserFetcher, BrowserManager, Fetcher, FetchUnavailable, HttpFetcher,
                       ImpersonateFetcher, origin_of)
from .models import ModelHit
from .sources import ChallengeError, Source, SourceError


class TTLCache:
    def __init__(self, ttl: float, max_items: int = 512):
        self.ttl, self.max_items = ttl, max_items
        self._data: dict[tuple, tuple[float, Any]] = {}

    def get(self, key: tuple) -> Any | None:
        item = self._data.get(key)
        if not item:
            return None
        ts, value = item
        if time.monotonic() - ts > self.ttl:
            self._data.pop(key, None)
            return None
        return value

    def set(self, key: tuple, value: Any) -> None:
        if len(self._data) >= self.max_items:
            oldest = min(self._data, key=lambda k: self._data[k][0])
            self._data.pop(oldest, None)
        self._data[key] = (time.monotonic(), value)

    def clear(self) -> None:
        self._data.clear()


class Breaker:

    def __init__(self, max_failures: int, cooldown_min: float):
        self.max_failures, self.cooldown = max_failures, timedelta(minutes=cooldown_min)
        self.fails: dict[str, int] = {}
        self.until: dict[str, datetime] = {}

    def is_open(self, name: str) -> datetime | None:
        t = self.until.get(name)
        if t and t > datetime.now():
            return t
        if t:
            self.until.pop(name, None)
            self.fails[name] = 0
        return None

    def ok(self, name: str):
        self.fails[name] = 0
        self.until.pop(name, None)

    def fail(self, name: str):
        self.fails[name] = self.fails.get(name, 0) + 1
        if self.max_failures and self.fails[name] >= self.max_failures:
            self.until[name] = datetime.now() + self.cooldown

    def reset(self, name: str | None = None):
        if name:
            self.fails.pop(name, None)
            self.until.pop(name, None)
        else:
            self.fails.clear()
            self.until.clear()


class Engine:
    def __init__(self, sources: dict[str, Source], client: httpx.AsyncClient, cfg: dict[str, Any]):
        self.sources = sources
        self.client = client
        self.cfg = cfg
        server = cfg["server"]
        self.timeout = float(server["request_timeout_seconds"])
        self.limit = int(server["per_source_limit"])
        self.cache = TTLCache(float(server["cache_ttl_seconds"]))
        self.breaker = Breaker(int(cfg["breaker"]["max_failures"]), float(cfg["breaker"]["cooldown_minutes"]))
        self._http = HttpFetcher(client)
        self._imp: ImpersonateFetcher | None = None
        bcfg = cfg["browser"]
        self.browser = BrowserManager(
            Path(server["data_dir"]) / "browser-profile",
            headless=bool(bcfg.get("headless", True)),
            executable_path=bcfg.get("executable_path") or None,
            nav_timeout=float(bcfg.get("nav_timeout_seconds", 25)),
        )

    def fetcher_for(self, src: Source) -> Fetcher:
        if src.mode == "impersonate":
            if self._imp is None:
                self._imp = ImpersonateFetcher(self.timeout, self.cfg["impersonate"].get("target", "chrome"))
            return self._imp
        if src.mode == "browser":
            return BrowserFetcher(self.browser, origin_of(src.homepage))
        return self._http

    def timeout_for(self, src: Source) -> float:
        return self.timeout * 4 if src.mode == "browser" else self.timeout

    async def _one(self, src: Source, query: str, page: int,
                   order: str | None = None) -> tuple[list[ModelHit], dict[str, Any]]:
        st: dict[str, Any] = {"name": src.name, "label": src.label, "mode": src.mode,
                              "status": "ok", "count": 0, "ms": 0, "link": src.search_link(query)}
        if src.mode == "link":
            st["status"] = "link"
            return [], st
        if not src.configured:
            st.update(status="skipped", error="нет ключа: " + ", ".join(src.missing_secrets))
            return [], st
        until = self.breaker.is_open(src.name)
        if until:
            st.update(status="paused", error=f"отключён до {until:%H:%M} после нескольких ошибок подряд")
            return [], st
        native = src.native_sort(order)
        key = (src.name, query.lower().strip(), page, native)
        cached = self.cache.get(key)
        if cached is not None:
            st.update(count=len(cached), cached=True)
            return [h.copy() for h in cached], st

        t0 = time.perf_counter()
        hits: list[ModelHit] = []
        try:
            fetcher = self.fetcher_for(src)
            kwargs = {"sort": native} if native else {}
            found = await asyncio.wait_for(src.search(fetcher, query, self.limit, page, **kwargs), self.timeout_for(src))
            hits = [h for h in found if h.url.startswith(("http://", "https://"))]
            self.cache.set(key, [h.copy() for h in hits])
            self.breaker.ok(src.name)
            st["count"] = len(hits)
        except ChallengeError as e:
            st.update(status="challenge", error=str(e), can_open=src.mode == "browser")
            self.breaker.fail(src.name)
        except FetchUnavailable as e:
            st.update(status="error", error=str(e))
        except asyncio.TimeoutError:
            st.update(status="error", error=f"таймаут {self.timeout_for(src):.0f} с")
            self.breaker.fail(src.name)
        except SourceError as e:
            st.update(status="error", error=str(e))
            self.breaker.fail(src.name)
        except httpx.HTTPError as e:
            st.update(status="error", error=f"сеть: {type(e).__name__}")
            self.breaker.fail(src.name)
        except Exception as e:
            st.update(status="error", error=f"{type(e).__name__}: {e}"[:180])
            self.breaker.fail(src.name)
        st["ms"] = round((time.perf_counter() - t0) * 1000)
        return hits, st

    async def stream(self, query: str, names: list[str], page: int = 1, order: str | None = None
                     ) -> AsyncIterator[tuple[list[ModelHit], list[dict[str, Any]], bool]]:
        chosen = [self.sources[n] for n in names if n in self.sources]
        statuses: dict[str, dict[str, Any]] = {
            s.name: {"name": s.name, "label": s.label, "mode": s.mode, "status": "pending",
                     "link": s.search_link(query)} for s in chosen
        }
        hits: list[ModelHit] = []
        tasks = {asyncio.ensure_future(self._one(s, query, page, order)): s.name for s in chosen}
        if not tasks:
            yield [], [], True
            return
        try:
            pending = set(tasks)
            while pending:
                done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
                for t in done:
                    h, st = t.result()
                    hits.extend(h)
                    statuses[tasks[t]] = st
                yield list(hits), [statuses[s.name] for s in chosen], not pending
        finally:
            for t in tasks:
                t.cancel()

    async def search(self, query: str, names: list[str], page: int = 1):
        last: tuple[list[ModelHit], list[dict[str, Any]]] = ([], [])
        async for hits, statuses, _ in self.stream(query, names, page):
            last = (hits, statuses)
        return last

    async def aclose(self):
        if self._imp is not None:
            await self._imp.aclose()
        await self.browser.aclose()
