from __future__ import annotations

import asyncio
import json
import logging
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlparse

import httpx

log = logging.getLogger("modelseek.fetch")

CHALLENGE_MARKERS = ("just a moment", "cf-chl", "challenge-platform", "один момент", "attention required")


@dataclass
class FetchResult:
    status: int
    text: str
    url: str = ""

    def json(self) -> Any:
        return json.loads(self.text)

    @property
    def looks_like_challenge(self) -> bool:
        head = self.text[:4000].lower()
        return self.status in (403, 429, 503) and any(m in head for m in CHALLENGE_MARKERS)


class FetchUnavailable(Exception):
    pass


def _with_params(url: str, params: dict | None) -> str:
    if not params:
        return url
    clean = {k: v for k, v in params.items() if v is not None}
    return url + ("&" if "?" in url else "?") + urlencode(clean, doseq=True)


class Fetcher:
    mode = "base"

    async def get(self, url: str, params: dict | None = None, headers: dict | None = None,
                  auth: tuple[str, str] | None = None) -> FetchResult:
        raise NotImplementedError

    async def post_json(self, url: str, payload: Any, headers: dict | None = None,
                        auth: tuple[str, str] | None = None) -> FetchResult:
        raise NotImplementedError


class HttpFetcher(Fetcher):
    mode = "http"

    def __init__(self, client: httpx.AsyncClient):
        self.client = client

    async def get(self, url, params=None, headers=None, auth=None):
        r = await self.client.get(url, params=params, headers=headers, auth=auth)
        return FetchResult(r.status_code, r.text, str(r.url))

    async def post_json(self, url, payload, headers=None, auth=None):
        r = await self.client.post(url, json=payload, headers=headers, auth=auth)
        return FetchResult(r.status_code, r.text, str(r.url))


class ImpersonateFetcher(Fetcher):
    mode = "impersonate"

    def __init__(self, timeout: float, target: str = "chrome"):
        try:
            from curl_cffi.requests import AsyncSession
        except ImportError as e:
            raise FetchUnavailable("не установлен curl_cffi (pip install curl_cffi)") from e
        self._session = AsyncSession(impersonate=target, timeout=timeout)

    async def get(self, url, params=None, headers=None, auth=None):
        r = await self._session.get(url, params=params, headers=headers, auth=auth)
        return FetchResult(r.status_code, r.text, str(r.url))

    async def post_json(self, url, payload, headers=None, auth=None):
        r = await self._session.post(url, json=payload, headers=headers, auth=auth)
        return FetchResult(r.status_code, r.text, str(r.url))

    async def aclose(self):
        await self._session.close()


class BrowserManager:

    def __init__(self, profile_dir: str | Path, headless: bool = True, executable_path: str | None = None,
                 nav_timeout: float = 25.0):
        self.profile_dir = Path(profile_dir)
        self.headless = headless
        self.executable_path = executable_path or None
        self.nav_timeout = nav_timeout
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._pw = None
        self._ctx = None
        self._ctx_headed = False
        self._pages: dict[str, Any] = {}
        self._lock: asyncio.Lock | None = None
        self.user_window: dict[str, Any] | None = None
        self._ua_file = self.profile_dir.parent / "browser-user-agent.txt"

    def _ensure_thread(self):
        if self._thread and self._thread.is_alive():
            return
        try:
            import playwright
        except ImportError as e:
            raise FetchUnavailable("не установлен playwright (запустите install-extras.bat "
                                   "или: pip install playwright && python -m playwright install chromium)") from e
        ready = threading.Event()

        def run():
            import sys
            loop = asyncio.ProactorEventLoop() if sys.platform == "win32" else asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self._loop = loop
            self._lock = asyncio.Lock()
            ready.set()
            loop.run_forever()

        self._thread = threading.Thread(target=run, name="modelseek-browser", daemon=True)
        self._thread.start()
        ready.wait(5)

    async def _call(self, coro_fn, *args):
        self._ensure_thread()
        fut = asyncio.run_coroutine_threadsafe(coro_fn(*args), self._loop)
        return await asyncio.wrap_future(fut)

    async def _start(self, headed: bool):
        from playwright.async_api import async_playwright

        if self._ctx is not None and self._ctx_headed == headed:
            return
        await self._close_ctx()
        if self._pw is None:
            self._pw = await async_playwright().start()
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        kwargs: dict[str, Any] = {
            "headless": not headed,
            "viewport": {"width": 1280, "height": 860},
            "args": ["--disable-blink-features=AutomationControlled"],
        }
        if self.executable_path:
            kwargs["executable_path"] = self.executable_path
        if not headed and self._ua_file.exists():
            kwargs["user_agent"] = self._ua_file.read_text(encoding="utf-8").strip()
        self._ctx = await self._pw.chromium.launch_persistent_context(str(self.profile_dir), **kwargs)
        self._ctx_headed = headed
        self._pages.clear()
        if headed:
            page = self._ctx.pages[0] if self._ctx.pages else await self._ctx.new_page()
            ua = await page.evaluate("navigator.userAgent")
            self._ua_file.parent.mkdir(parents=True, exist_ok=True)
            self._ua_file.write_text(ua, encoding="utf-8")
        elif not self._ua_file.exists():
            page = self._ctx.pages[0] if self._ctx.pages else await self._ctx.new_page()
            ua = (await page.evaluate("navigator.userAgent")).replace("HeadlessChrome", "Chrome")
            await self._close_ctx()
            self._ua_file.parent.mkdir(parents=True, exist_ok=True)
            self._ua_file.write_text(ua, encoding="utf-8")
            await self._start(headed=False)

    async def _close_ctx(self):
        if self._ctx is not None:
            try:
                await self._ctx.close()
            except Exception:
                pass
        self._ctx = None
        self._pages.clear()

    async def _page_for(self, origin: str):
        page = self._pages.get(origin)
        if page is None or page.is_closed():
            page = await self._ctx.new_page()
            await page.goto(origin, wait_until="domcontentloaded", timeout=self.nav_timeout * 1000)
            self._pages[origin] = page
        return page

    async def _fetch(self, origin: str, url: str, method: str, body: str | None, headers: dict):
        async with self._lock:
            if self.user_window and not self.user_window.get("done"):
                return FetchResult(409, "browser is busy: waiting for the user to pass the check", url)
            await self._start(headed=False)
            page = await self._page_for(origin)
            js = """async ([url, method, body, headers]) => {
                const r = await fetch(url, {method, body, headers, credentials: 'include'});
                return [r.status, await r.text(), r.url];
            }"""
            status, text, final = await page.evaluate(js, [url, method, body, headers])
            if status in (403, 503) and any(m in text[:4000].lower() for m in CHALLENGE_MARKERS):
                await page.goto(origin, wait_until="domcontentloaded", timeout=self.nav_timeout * 1000)
                status, text, final = await page.evaluate(js, [url, method, body, headers])
            return FetchResult(int(status), text, final)

    async def _open_for_user(self, origin: str):
        async with self._lock:
            await self._start(headed=True)
            page = await self._ctx.new_page()
            self.user_window = {"origin": origin, "done": False}
            await page.goto(origin, wait_until="domcontentloaded", timeout=self.nav_timeout * 1000)
            await page.bring_to_front()

        async def watch():
            try:
                while self._ctx is not None and self._ctx_headed and any(not p.is_closed() for p in self._ctx.pages):
                    await asyncio.sleep(1)
            finally:
                await self._finish_user()

        asyncio.ensure_future(watch())

    async def _finish_user(self):
        async with self._lock:
            if self.user_window:
                self.user_window["done"] = True
            if self._ctx_headed:
                await self._close_ctx()

    async def _shutdown(self):
        await self._close_ctx()
        if self._pw is not None:
            await self._pw.stop()
            self._pw = None

    async def fetch(self, origin: str, url: str, method: str = "GET", body: str | None = None,
                    headers: dict | None = None) -> FetchResult:
        return await self._call(self._fetch, origin, url, method, body, headers or {})

    async def open_for_user(self, origin: str):
        await self._call(self._open_for_user, origin)

    async def finish_user(self):
        if self._loop:
            await self._call(self._finish_user)

    def status(self) -> dict[str, Any]:
        return {
            "running": self._ctx is not None,
            "headed": self._ctx_headed,
            "waiting_user": bool(self.user_window and not self.user_window.get("done")),
            "origin": (self.user_window or {}).get("origin"),
        }

    async def aclose(self):
        if self._loop and self._thread and self._thread.is_alive():
            try:
                await self._call(self._shutdown)
            finally:
                self._loop.call_soon_threadsafe(self._loop.stop)


class BrowserFetcher(Fetcher):
    mode = "browser"

    def __init__(self, manager: BrowserManager, origin: str):
        self.manager = manager
        self.origin = origin.rstrip("/") + "/"

    async def get(self, url, params=None, headers=None, auth=None):
        if auth:
            raise FetchUnavailable("режим browser не поддерживает логин/ключ — используйте http")
        return await self.manager.fetch(self.origin, _with_params(url, params), "GET", None, headers)

    async def post_json(self, url, payload, headers=None, auth=None):
        if auth:
            raise FetchUnavailable("режим browser не поддерживает логин/ключ — используйте http")
        h = {"Content-Type": "application/json", **(headers or {})}
        return await self.manager.fetch(self.origin, url, "POST", json.dumps(payload), h)


def origin_of(url: str) -> str:
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}"
