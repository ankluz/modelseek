from __future__ import annotations

import asyncio
import io
from collections import OrderedDict
from typing import Any

import httpx

from .models import ModelHit


class ImageDeduper:
    def __init__(self, client: httpx.AsyncClient, enabled: bool, max_images: int = 60, threshold: int = 6):
        self.client = client
        self.max_images = max_images
        self.threshold = threshold
        self._cache: OrderedDict[str, int | None] = OrderedDict()
        try:
            import importlib
            importlib.invalidate_caches()
            import PIL
            self.available = True
        except ImportError:
            self.available = False
        self.enabled = enabled and self.available

    @staticmethod
    def dhash(data: bytes) -> int:
        from PIL import Image

        img = Image.open(io.BytesIO(data)).convert("L").resize((9, 8), Image.Resampling.LANCZOS)
        px = list(img.getdata())
        bits = 0
        for row in range(8):
            for col in range(8):
                bits = (bits << 1) | (px[row * 9 + col] > px[row * 9 + col + 1])
        return bits

    async def _hash(self, url: str) -> int | None:
        if url in self._cache:
            return self._cache[url]
        h: int | None = None
        try:
            if not url.startswith("data:"):
                r = await self.client.get(url, timeout=8)
                if r.status_code == 200 and len(r.content) < 5_000_000:
                    h = await asyncio.to_thread(self.dhash, r.content)
        except Exception:
            h = None
        self._cache[url] = h
        if len(self._cache) > 3000:
            self._cache.popitem(last=False)
        return h

    async def merge(self, hits: list[ModelHit]) -> list[ModelHit]:
        if not self.enabled:
            return hits
        top = [h for h in hits[: self.max_images] if h.thumbnail]
        hashes = await asyncio.gather(*(self._hash(h.thumbnail) for h in top))
        hmap: dict[str, int] = {h.key: v for h, v in zip(top, hashes) if v is not None}
        out: list[ModelHit] = []
        leads: list[tuple[ModelHit, int]] = []
        for h in hits:
            hv = hmap.get(h.key)
            if hv is not None:
                lead = next((l for l, lv in leads
                             if l.source != h.source and bin(lv ^ hv).count("1") <= self.threshold), None)
                if lead is not None:
                    lead.also_on.append({"source": h.source, "url": h.url, "key": h.key})
                    lead.also_on.extend(h.also_on)
                    continue
                leads.append((h, hv))
            out.append(h)
        return out

    def status(self) -> dict[str, Any]:
        return {"enabled": self.enabled, "available": self.available}
