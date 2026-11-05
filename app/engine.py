from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from .fetchers import (BrowserFetcher, BrowserManager, Fetcher, FetchUnavailable, HttpFetcher,
                       ImpersonateFetcher, Planned, PlanFetcher, PlannedRequest, ReplayFetcher, origin_of)
from .models import ModelHit, clean_hit
from .netguard import BlockedRequest, NetGuard, ResponseTooLarge, endpoint
from .sources import GUEST, ChallengeError, CustomSource, GuestAccess, Source, SourceError


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
    def __init__(self, sources: dict[str, Source], client: httpx.AsyncClient, cfg: dict[str, Any],
                 guard: NetGuard | None = None):
        self.sources = sources
        self.guard = guard or NetGuard()
        self.client = client
        self.cfg = cfg
        server = cfg["server"]
        self.timeout = float(server["request_timeout_seconds"])
        self.limit = int(server["per_source_limit"])
        self.cache = TTLCache(float(server["cache_ttl_seconds"]))
        self.breaker = Breaker(int(cfg["breaker"]["max_failures"]), float(cfg["breaker"]["cooldown_minutes"]))
        self._http = HttpFetcher(client, guard=self.guard)
        self._imp: ImpersonateFetcher | None = None
        bcfg = cfg["browser"]
        self.browser = BrowserManager(
            Path(server["data_dir"]) / "browser-profile",
            headless=bool(bcfg.get("headless", True)),
            executable_path=bcfg.get("executable_path") or None,
            nav_timeout=float(bcfg.get("nav_timeout_seconds", 25)),
            guard=self.guard,
        )

    def fetcher_for(self, src: Source) -> Fetcher:
        if src.mode == "impersonate":
            if self._imp is None:
                self._imp = ImpersonateFetcher(self.timeout, self.cfg["impersonate"].get("target", "chrome"),
                                               guard=self.guard)
            return self._imp
        if src.mode == "browser":
            return BrowserFetcher(self.browser, origin_of(src.homepage))
        return self._http

    async def safe_hits(self, found: list[ModelHit], src: Source | None = None) -> tuple[list[ModelHit], int]:
        """Clean hits and drop links into the local network; thumbnails pointing there are removed.

        Only a user's own source may point into the local network, and only at its own host and port.
        """
        hits = [c for h in found if (c := clean_hit(h)) is not None]
        own = endpoint(str(src.cfg.get("url") or "")) if isinstance(src, CustomSource) else None
        hosts: dict[tuple, bool] = {}

        async def ok(url: str) -> bool:
            ep = endpoint(url)
            if ep is None:
                return False
            if ep not in hosts:
                hosts[ep] = (await self.guard.allowed(url, use_trust=False)
                             or (own is not None and ep[1:] == own[1:]))
            return hosts[ep]

        out, dropped = [], len(found) - len(hits)
        for h in hits:
            if not await ok(h.url):
                dropped += 1
                continue
            if h.thumbnail and not h.thumbnail.startswith("data:") and not await ok(h.thumbnail):
                h.thumbnail = None
            out.append(h)
        return out, dropped

    def timeout_for(self, src: Source) -> float:
        return self.timeout * 4 if src.mode == "browser" else self.timeout

    async def _one(self, src: Source, query: str, page: int, order: str | None = None,
                   guest: GuestAccess | None = None) -> tuple[list[ModelHit], dict[str, Any]]:
        """guest: runs with the visitor's keys. Their failures never pause a source for the owner, and an
        answer their browser fetched itself is used for them only and never cached."""
        if guest is not None:
            GUEST.set(guest)  # this task's own context: the keys reach Source.secret and nothing else
        st: dict[str, Any] = {"name": src.name, "label": src.label, "mode": src.mode,
                              "status": "ok", "count": 0, "ms": 0, "link": src.search_link(query)}
        if src.mode == "link":
            st["status"] = "link"
            return [], st
        if not src.configured:
            hint = ("добавьте свой ключ в настройках" if guest is not None
                    else "нет ключа: " + ", ".join(src.missing_secrets))
            st.update(status="skipped", error=hint)
            return [], st
        until = self.breaker.is_open(src.name)
        if until:
            st.update(status="paused", error=f"отключён до {until:%H:%M} после нескольких ошибок подряд")
            return [], st
        native = src.native_sort(order)
        fetched = guest.fetched.get(src.name) if guest is not None else None
        key = (src.name, query.lower().strip(), page, native)
        if fetched is None:
            cached = self.cache.get(key)
            if cached is not None:
                st.update(count=len(cached), cached=True)
                return [h.copy() for h in cached], st

        def fail():
            if guest is None:
                self.breaker.fail(src.name)

        t0 = time.perf_counter()
        hits: list[ModelHit] = []
        try:
            if fetched is not None:
                fetcher: Fetcher = ReplayFetcher(*fetched)
                st["via"] = "browser"
            else:
                fetcher = self.fetcher_for(src)
            kwargs = {"sort": native} if native else {}
            found = await asyncio.wait_for(src.search(fetcher, query, self.limit, page, **kwargs), self.timeout_for(src))
            hits, dropped = await self.safe_hits(found, src)
            if fetched is None:
                self.cache.set(key, [h.copy() for h in hits])
            if guest is None:
                self.breaker.ok(src.name)
            st["count"] = len(hits)
            if dropped:
                st["dropped"] = dropped
        except ChallengeError as e:
            st.update(status="challenge", error=str(e), can_open=src.mode == "browser" and guest is None)
            fail()
        except FetchUnavailable as e:
            st.update(status="error", error=str(e))
        except asyncio.TimeoutError:
            st.update(status="error", error=f"таймаут {self.timeout_for(src):.0f} с")
            fail()
        except (SourceError, BlockedRequest, ResponseTooLarge) as e:
            st.update(status="error", error=str(e)[:200])
            fail()
        except httpx.HTTPError as e:
            st.update(status="error", error=f"сеть: {type(e).__name__}")
            fail()
        except Exception as e:
            st.update(status="error", error=f"{type(e).__name__}: {e}"[:180])
            fail()
        st["ms"] = round((time.perf_counter() - t0) * 1000)
        return hits, st

    async def plan(self, src: Source, query: str, page: int, order: str | None,
                   guest: GuestAccess) -> PlannedRequest | None:
        """The request a guest's browser may send itself: only to a site that allows it (CORS), in plain
        http mode, and only with the guest's own keys, never the owner's."""
        if not src.browser_direct or src.mode != "http" or not src.enabled:
            return None
        token = GUEST.set(guest)
        try:
            if not src.guest_keyed():
                return None
            native = src.native_sort(order)
            kwargs = {"sort": native} if native else {}
            try:
                await src.search(PlanFetcher(), query, self.limit, page, **kwargs)
            except Planned as p:
                return p.request
            except Exception:
                return None
            return None
        finally:
            GUEST.reset(token)

    async def stream(self, query: str, names: list[str], page: int = 1, order: str | None = None,
                     guest: GuestAccess | None = None
                     ) -> AsyncIterator[tuple[list[ModelHit], list[dict[str, Any]], bool]]:
        chosen = [self.sources[n] for n in names if n in self.sources]
        statuses: dict[str, dict[str, Any]] = {
            s.name: {"name": s.name, "label": s.label, "mode": s.mode, "status": "pending",
                     "link": s.search_link(query)} for s in chosen
        }
        hits: list[ModelHit] = []
        tasks = {asyncio.ensure_future(self._one(s, query, page, order, guest)): s.name for s in chosen}
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
