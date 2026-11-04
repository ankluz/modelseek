from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

import httpx

from .config import Settings
from .engine import Engine
from .imagededupe import ImageDeduper
from .installer import Jobs
from .pipeline import Pipeline
from .scheduler import Notifier, Scheduler
from .semantic import Embedder
from .sources import build_sources
from .storage import Storage
from .translate import Translator

log = logging.getLogger("modelseek.runtime")


class Runtime:
    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None,
                 db_path: str | Path | None = None, start_scheduler: bool = True):
        self.settings = settings
        self.transport = transport
        self.db_path = db_path
        self.start_scheduler = start_scheduler
        self.jobs = Jobs()
        self.loop: asyncio.AbstractEventLoop | None = None
        self._lock = asyncio.Lock()
        self.cfg: dict[str, Any] = {}
        self.version = 0

    async def start(self):
        self.loop = asyncio.get_running_loop()
        self._build()
        self.storage = Storage(self.db_path or Path(self.settings.data_dir) / "modelseek.db")
        self._wire()
        self.scheduler = Scheduler(self.cfg, self.storage, self.pipeline, self.notifier, self._open_url())
        if self.start_scheduler:
            self.scheduler.start()

    def _open_url(self) -> str:
        return self.cfg["notify"].get("open_url") or "http://127.0.0.1:8088"

    def _build(self, prev_embedder: Embedder | None = None):
        cfg = self.settings.effective()
        client = httpx.AsyncClient(
            transport=self.transport,
            timeout=httpx.Timeout(float(cfg["server"]["request_timeout_seconds"])),
            headers={"User-Agent": cfg["server"]["user_agent"], "Accept-Language": "en"},
            follow_redirects=True,
        )
        self.cfg = cfg
        self.client = client
        self.sources = build_sources(cfg)
        self.engine = Engine(self.sources, client, cfg)
        if (prev_embedder is not None and prev_embedder.available
                and prev_embedder.enabled == bool(cfg["semantic"].get("enabled"))
                and prev_embedder.model_name == cfg["semantic"].get("model")):
            self.embedder = prev_embedder
        else:
            self.embedder = Embedder(cfg["semantic"], cache_dir=str(Path(self.settings.data_dir) / "models"))
        self.translator = Translator(cfg["translate"], client)
        self.deduper = ImageDeduper(client, bool(cfg["ranking"].get("dedupe_images")))
        self.notifier = Notifier(cfg["notify"], client)
        self.version += 1

    def _wire(self):
        self.pipeline = Pipeline(self.cfg, self.engine, self.storage, self.embedder, self.translator, self.deduper)

    async def reload(self):
        async with self._lock:
            old_client, old_engine = self.client, self.engine
            self._build(prev_embedder=self.embedder)
            self._wire()
            self.scheduler.configure(self.cfg, self.pipeline, self.notifier, self._open_url())

        async def close_later():
            await asyncio.sleep(30)
            try:
                await old_engine.aclose()
                await old_client.aclose()
            except Exception:
                log.exception("closing old runtime")

        asyncio.ensure_future(close_later())

    def reload_soon(self):
        if self.loop:
            self.loop.call_soon_threadsafe(lambda: asyncio.ensure_future(self.reload()))

    async def close(self):
        await self.scheduler.stop()
        await self.engine.aclose()
        await self.client.aclose()
        self.storage.close()
