from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from .config import env
from .pipeline import Pipeline, SearchParams
from .storage import Storage

log = logging.getLogger("modelseek.subs")

INTERVALS = {
    60: "каждый час",
    360: "каждые 6 часов",
    720: "дважды в день",
    1440: "раз в день",
    10080: "раз в неделю",
}


class Notifier:
    def __init__(self, cfg: dict[str, Any], client: httpx.AsyncClient):
        self.kind = (cfg.get("kind") or "").lower()
        self.url = cfg.get("url") or ""
        self.token = env(cfg.get("token_env"))
        self.chat_id = str(cfg.get("chat_id") or "")
        self.client = client

    @property
    def enabled(self) -> bool:
        if self.kind == "telegram":
            return bool(self.token and self.chat_id)
        return self.kind in ("ntfy", "webhook") and bool(self.url)

    async def send(self, title: str, text: str, link: str | None = None, items: list[dict] | None = None) -> str | None:
        if not self.enabled:
            return None
        try:
            if self.kind == "ntfy":
                params = {"title": title, "tags": "printer"}
                if link:
                    params["click"] = link
                headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
                r = await self.client.post(self.url, params=params, content=text.encode("utf-8"), headers=headers)
            elif self.kind == "telegram":
                r = await self.client.post(f"https://api.telegram.org/bot{self.token}/sendMessage",
                                           json={"chat_id": self.chat_id, "text": f"{title}\n{text}",
                                                 "disable_web_page_preview": True})
            else:
                r = await self.client.post(self.url, json={"title": title, "text": text, "link": link,
                                                           "items": items or []})
            return None if r.status_code < 400 else f"HTTP {r.status_code}"
        except httpx.HTTPError as e:
            return f"{type(e).__name__}"


class Scheduler:
    def __init__(self, cfg: dict[str, Any], storage: Storage, pipeline: Pipeline, notifier: Notifier,
                 base_url: str = "http://127.0.0.1:8088"):
        self.storage = storage
        self._task: asyncio.Task | None = None
        self._running: set[int] = set()
        self.configure(cfg, pipeline, notifier, base_url)

    def configure(self, cfg: dict[str, Any], pipeline: Pipeline, notifier: Notifier, base_url: str):
        self.cfg, self.pipeline, self.notifier = cfg, pipeline, notifier
        self.every = float(cfg["subscriptions"]["check_every_seconds"])
        self.keep = int(cfg["subscriptions"]["max_new_kept"])
        self.base_url = base_url

    def start(self):
        if self._task is None:
            self._task = asyncio.create_task(self._loop())

    async def stop(self):
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    async def _loop(self):
        await asyncio.sleep(5)
        while True:
            try:
                for sub in (self.storage.due_subs() if self.every > 0 else []):
                    await self.run_sub(sub["id"])
            except Exception:
                log.exception("subscription loop error")
            await asyncio.sleep(max(15.0, self.every))

    async def run_sub(self, sid: int) -> dict[str, Any]:
        sub = self.storage.sub(sid)
        if not sub or sid in self._running:
            return {"ok": False, "error": "уже выполняется" if sub else "нет такой подписки"}
        self._running.add(sid)
        try:
            sp = SearchParams.from_dict({**sub["params"], "query": sub["query"], "page": 1, "site_sort": "newest"})
            snap = await self.pipeline.final(sp, record_history=False)
            results = snap.get("results", [])
            statuses = snap.get("sources", [])
            errors = [f"{s['label']}: {s.get('error')}" for s in statuses if s["status"] in ("error", "challenge")]
            first_run = not self.storage.sub_baseline(sid)
            ok_sources = [s["name"] for s in statuses if s["status"] == "ok"]
            new = self.storage.record_sub_items(sid, results, ok_sources, keep=self.keep)
            self.storage.mark_sub_run(sid, sub["interval_min"], "; ".join(errors) or None)
            if new:
                new_items = self.storage.sub_items(sid, only_new=True)[:new]
                names = "\n".join(f"• {h['title']}" for h in new_items[:5])
                more = f"\n…и ещё {new - 5}" if new > 5 else ""
                err = await self.notifier.send(f"modelseek: {new} новых по «{sub['query']}»", names + more,
                                               link=f"{self.base_url}/#subs/{sid}",
                                               items=[{"title": h["title"], "url": h["url"]} for h in new_items[:20]])
                if err:
                    log.warning("notify failed: %s", err)
            return {"ok": True, "new": new, "total": len(results), "first_run": first_run, "errors": errors}
        except Exception as e:
            self.storage.mark_sub_run(sid, sub["interval_min"], f"{type(e).__name__}: {e}"[:200])
            log.exception("subscription %s failed", sid)
            return {"ok": False, "error": str(e)}
        finally:
            self._running.discard(sid)
