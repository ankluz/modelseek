from __future__ import annotations

import asyncio
import logging
import threading
from collections import OrderedDict
from typing import Any

from .models import ModelHit

log = logging.getLogger("modelseek.semantic")


class Embedder:
    def __init__(self, cfg: dict[str, Any], cache_dir: str | None = None):
        self.cache_dir = cache_dir
        self.enabled = bool(cfg.get("enabled"))
        self.model_name = cfg.get("model")
        self._model = None
        self._error: str | None = None
        self._lock = threading.Lock()
        self._cache: OrderedDict[str, Any] = OrderedDict()
        self._max = 5000
        if self.enabled:
            try:
                import importlib
                importlib.invalidate_caches()
                import fastembed
            except ImportError:
                self._error = "не установлен модуль fastembed — Настройки → Компоненты"

    @property
    def available(self) -> bool:
        return self.enabled and self._error is None

    def status(self) -> dict[str, Any]:
        return {"enabled": self.enabled, "available": self.available, "loaded": self._model is not None,
                "model": self.model_name, "error": self._error}

    def _load(self):
        with self._lock:
            if self._model is None:
                from fastembed import TextEmbedding
                log.info("loading embedding model %s", self.model_name)
                kwargs = {"cache_dir": self.cache_dir} if self.cache_dir else {}
                self._model = TextEmbedding(model_name=self.model_name, **kwargs)
        return self._model

    def _embed(self, texts: list[str]) -> list[Any]:
        import numpy as np

        missing = [t for t in dict.fromkeys(texts) if t not in self._cache]
        if missing:
            vecs = list(self._load().embed(missing))
            for t, v in zip(missing, vecs):
                v = np.asarray(v, dtype="float32")
                n = float(np.linalg.norm(v)) or 1.0
                self._cache[t] = v / n
                if len(self._cache) > self._max:
                    self._cache.popitem(last=False)
        for t in texts:
            self._cache.move_to_end(t)
        return [self._cache[t] for t in texts]

    def _scores_sync(self, query: str, hits: list[ModelHit]) -> dict[str, float]:
        texts = [f"{h.title}. {' '.join(h.tags[:8])}".strip() for h in hits]
        vecs = self._embed([query] + texts)
        q = vecs[0]
        out = {}
        for h, v in zip(hits, vecs[1:]):
            cos = float((q * v).sum())
            out[h.key] = max(0.0, min(1.0, (cos - 0.15) / 0.6))
        return out

    async def scores(self, query: str, hits: list[ModelHit]) -> dict[str, float] | None:
        if not self.available or not hits:
            return None
        try:
            return await asyncio.to_thread(self._scores_sync, query, hits)
        except Exception as e:
            self._error = f"{type(e).__name__}: {e}"[:200]
            log.warning("semantic disabled: %s", self._error)
            return None
