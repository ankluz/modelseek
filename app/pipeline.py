from __future__ import annotations

import time
from collections.abc import AsyncIterator
from dataclasses import asdict, dataclass, field
from typing import Any

from .engine import Engine
from .imagededupe import ImageDeduper
from .ranking import SIGNALS, Profile, apply_filters, facets, normalize_filters, rank, sort_hits
from .semantic import Embedder
from .storage import Storage
from .translate import Translator

FILTER_KEYS = ("price", "max_price", "min_likes", "min_downloads", "min_makes", "days", "license",
               "include", "exclude", "exclude_authors", "has_image", "hide_seen", "free_only")


def _float(v: Any, default: float) -> float:
    try:
        return float(v) if v not in (None, "") else default
    except (TypeError, ValueError):
        return default


@dataclass
class SearchParams:
    query: str
    sources: list[str] | None = None
    page: int = 1
    sort: str = "score"
    weights: dict[str, float] = field(default_factory=dict)
    source_weights: dict[str, float] = field(default_factory=dict)
    half_life: float | None = None
    filters: dict[str, Any] = field(default_factory=dict)
    dedupe: bool = True

    @classmethod
    def from_mapping(cls, p: Any, cfg: dict[str, Any]) -> "SearchParams":
        r = cfg["ranking"]
        sources = None
        if "sources" in p:
            sources = [n for n in str(p.get("sources") or "").split(",") if n]
        filters = dict(cfg["filters"])
        for k in FILTER_KEYS:
            if k in p and p.get(k) is not None:
                filters[k] = p.get(k)
        sw = {k[3:]: _float(v, 1.0) for k, v in p.items() if k.startswith("sw_")}
        return cls(
            query=str(p.get("q") or p.get("query") or "").strip(),
            sources=sources,
            page=max(1, min(20, int(_float(p.get("page"), 1)))),
            sort=str(p.get("sort") or "score"),
            weights={k: _float(p.get(f"w_{k}"), r["weights"].get(k, 0)) for k in SIGNALS},
            source_weights=sw,
            half_life=_float(p.get("half_life"), r["freshness_half_life_days"]),
            filters=normalize_filters(filters),
            dedupe=str(p.get("dedupe", "1")) not in ("0", "false", "off"),
        )

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "SearchParams":
        known = {k: d[k] for k in cls.__dataclass_fields__ if k in d}
        sp = cls(**known)
        sp.filters = normalize_filters(sp.filters)
        return sp

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class Pipeline:
    def __init__(self, cfg: dict[str, Any], engine: Engine, storage: Storage, embedder: Embedder,
                 translator: Translator, deduper: ImageDeduper):
        self.cfg, self.engine, self.storage = cfg, engine, storage
        self.embedder, self.translator, self.deduper = embedder, translator, deduper

    def _names(self, sp: SearchParams) -> list[str]:
        srcs = self.engine.sources
        if sp.sources is not None:
            return [n for n in sp.sources if n in srcs]
        return [n for n, s in srcs.items() if s.enabled]

    async def run(self, sp: SearchParams, record_history: bool = True) -> AsyncIterator[dict[str, Any]]:
        t0 = time.perf_counter()
        r = self.cfg["ranking"]
        translated = await self.translator.to_english(sp.query)
        site_query = translated or sp.query
        hidden = self.storage.hidden_keys()
        seen = self.storage.seen_keys()
        favs = self.storage.favorite_keys()
        profile = Profile.from_hits(self.storage.favorite_hits()) if sp.weights.get("personal", 0) > 0 else None
        source_weights = {n: s.weight for n, s in self.engine.sources.items()} | sp.source_weights
        sem_cache: dict[str, float] = {}

        async for hits, statuses, done in self.engine.stream(site_query, self._names(sp), sp.page):
            filtered = apply_filters(hits, sp.filters, hidden, seen)
            todo = [h for h in filtered if h.key not in sem_cache]
            if todo:
                scores = await self.embedder.scores(sp.query, todo)
                if scores:
                    sem_cache.update(scores)
            semantic = sem_cache if self.embedder.available and sem_cache else None
            ranked = rank(filtered, site_query, sp.weights, source_weights,
                          half_life_days=sp.half_life or r["freshness_half_life_days"],
                          downloads_factor=r["downloads_factor"],
                          do_dedupe=sp.dedupe and r["dedupe"], semantic=semantic, profile=profile,
                          alt_query=sp.query if translated else None)
            if done and sp.dedupe:
                ranked = await self.deduper.merge(ranked)
            topics = facets(ranked, site_query) if sp.page == 1 else []
            ranked = sort_hits(ranked, sp.sort)
            results = []
            for h in ranked:
                d = h.to_dict()
                d["favorite"] = favs.get(h.key)
                d["seen"] = h.key in seen
                results.append(d)
            if done and record_history and sp.page == 1:
                self.storage.add_history(sp.query, len(results))
            yield {
                "query": sp.query,
                "translated": translated,
                "page": sp.page,
                "done": done,
                "took_ms": round((time.perf_counter() - t0) * 1000),
                "sources": statuses,
                "facets": topics,
                "results": results,
            }

    async def final(self, sp: SearchParams, record_history: bool = True) -> dict[str, Any]:
        last: dict[str, Any] = {}
        async for snap in self.run(sp, record_history):
            last = snap
        return last
