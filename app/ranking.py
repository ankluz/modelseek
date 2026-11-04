from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from typing import Any, Iterable

from .models import ModelHit

TOKEN_RE = re.compile(r"[^\W_]+", re.UNICODE)
NEUTRAL = 0.3
SIGNALS = ("relevance", "semantic", "source_rank", "popularity", "freshness", "personal")


def tokens(text: str) -> list[str]:
    return [t for t in TOKEN_RE.findall((text or "").lower()) if len(t) > 1 or t.isdigit()]


CYRILLIC = re.compile(r"[а-яё]")
RU_ENDINGS = tuple(sorted("""
ями ами ого его ому ему ыми ими ов ев ей ой ый ий ая яя ое ее ые ие ам ям ах ях ом ем ую юю а я о е и ы у ю ь й
""".split(), key=len, reverse=True))
EN_SUFFIXES = (("ing", 4), ("ers", 4), ("ed", 4), ("er", 4), ("es", 3), ("s", 3))


def stem(word: str) -> str:
    if not word.isalpha():
        return word
    if CYRILLIC.search(word):
        for ending in RU_ENDINGS:
            if word.endswith(ending) and len(word) - len(ending) >= 3:
                return word[:-len(ending)]
        return word
    for suffix, min_stem in EN_SUFFIXES:
        if word.endswith(suffix) and len(word) - len(suffix) >= min_stem and not word.endswith("ss"):
            word = word[:-len(suffix)]
            break
    return word[:-1] if len(word) > 3 and word.endswith("e") else word


def _token_hit(q: str, words: set[str]) -> float:
    if q in words:
        return 1.0
    target = stem(q)
    return 0.8 if any(stem(w) == target for w in words) else 0.0


def matches_query(query: str, hit: ModelHit, alt_query: str | None = None) -> bool:
    words = set(tokens(hit.title)) | {t for tag in hit.tags for t in tokens(tag)}
    for candidate in (query, alt_query):
        parts = tokens(candidate or "")
        if parts and sum(_token_hit(p, words) > 0 for p in parts) * 2 >= len(parts):
            return True
    return not tokens(query) and not tokens(alt_query or "")


def relevance(query: str, hit: ModelHit) -> float:
    q = tokens(query)
    if not q:
        return NEUTRAL
    title_words = set(tokens(hit.title))
    tag_words = {t for tag in hit.tags for t in tokens(tag)}
    in_title = sum(_token_hit(t, title_words) for t in q) / len(q)
    in_tags = sum(_token_hit(t, tag_words) for t in q) / len(q) if tag_words else 0.0
    fuzzy = SequenceMatcher(None, query.lower(), (hit.title or "").lower()).ratio()
    return min(1.0, 0.65 * in_title + 0.15 * in_tags + 0.20 * fuzzy)


def source_rank(hit: ModelHit) -> float:
    return 1.0 / (1.0 + hit.position / 8.0)


def freshness(hit: ModelHit, half_life_days: float, now: datetime) -> float:
    if not hit.published:
        return NEUTRAL
    age = max(0.0, (now - hit.published).total_seconds() / 86400)
    return 0.5 ** (age / max(1.0, half_life_days))


def _raw_popularity(hit: ModelHit, dl_factor: float) -> float | None:
    if hit.likes is None and hit.downloads is None and hit.views is None and hit.makes is None:
        return None
    likes = hit.likes or 0
    dl = hit.downloads if hit.downloads is not None else (hit.views or 0) * 0.1
    return math.log1p(likes + dl_factor * dl + 3 * (hit.makes or 0))


def normalize_weights(w: dict[str, float]) -> dict[str, float]:
    clean = {k: max(0.0, float(w.get(k, 0) or 0)) for k in SIGNALS}
    total = sum(clean.values())
    if total <= 0:
        return {k: 0.0 for k in SIGNALS}
    return {k: v / total for k, v in clean.items()}



@dataclass
class Profile:
    authors: set[str] = field(default_factory=set)
    tags: Counter = field(default_factory=Counter)

    @classmethod
    def from_hits(cls, hits: Iterable[ModelHit]) -> "Profile":
        p = cls()
        for h in hits:
            if h.author:
                p.authors.add(h.author.lower())
            for t in h.tags:
                p.tags[t.lower()] += 1
            for t in set(tokens(h.title)):
                if len(t) >= 4:
                    p.tags[t] += 1
        return p

    def score(self, h: ModelHit) -> float:
        if not self.authors and not self.tags:
            return 0.0
        a = 1.0 if h.author and h.author.lower() in self.authors else 0.0
        words = {t.lower() for t in h.tags} | {t for t in tokens(h.title) if len(t) >= 4}
        overlap = sum(min(self.tags.get(w, 0), 3) for w in words) / 6.0
        return min(1.0, 0.6 * a + 0.4 * min(1.0, overlap))



def _words(v: Any) -> list[str]:
    if isinstance(v, str):
        v = v.split(",")
    return [w.strip().lower() for w in v or [] if str(w).strip()]


def normalize_filters(f: dict[str, Any]) -> dict[str, Any]:
    f = dict(f or {})
    price = f.get("price") or ("free" if f.get("free_only") in (True, "1", "true", "on") else "any")
    return {
        "price": price if price in ("any", "free", "paid") else "any",
        "max_price": float(f["max_price"]) if f.get("max_price") not in (None, "", 0, "0") else None,
        "min_likes": int(float(f.get("min_likes") or 0)),
        "min_downloads": int(float(f.get("min_downloads") or 0)),
        "min_makes": int(float(f.get("min_makes") or 0)),
        "days": int(float(f.get("days") or 0)),
        "license": f.get("license") if f.get("license") in ("commercial", "commercial_strict") else "any",
        "include": _words(f.get("include") or f.get("include_words")),
        "exclude": _words(f.get("exclude") or f.get("exclude_words")),
        "exclude_authors": _words(f.get("exclude_authors")),
        "has_image": f.get("has_image") in (True, "1", "true", "on"),
        "hide_seen": f.get("hide_seen") in (True, "1", "true", "on"),
    }


def _contains_word(words: set[str], w: str) -> bool:
    parts = tokens(w)
    if len(parts) > 1:
        return all(_token_hit(p, words) > 0 for p in parts)
    return bool(parts) and _token_hit(parts[0], words) > 0


def apply_filters(hits: Iterable[ModelHit], f: dict[str, Any], hidden: set[str] | None = None,
                  seen: set[str] | None = None, now: datetime | None = None) -> list[ModelHit]:
    f = normalize_filters(f)
    hidden = hidden or set()
    seen = seen or set()
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=f["days"]) if f["days"] else None
    out = []
    for h in hits:
        if h.key in hidden:
            continue
        if f["hide_seen"] and h.key in seen:
            continue
        words = set(tokens(h.title)) | {t for tag in h.tags for t in tokens(tag)}
        if f["exclude"] and any(_contains_word(words, w) for w in f["exclude"]):
            continue
        if f["include"] and not all(_contains_word(words, w) for w in f["include"]):
            continue
        if f["exclude_authors"] and h.author and h.author.lower() in f["exclude_authors"]:
            continue
        if f["min_likes"] and (h.likes or 0) < f["min_likes"]:
            continue
        if f["min_downloads"] and (h.downloads or 0) < f["min_downloads"]:
            continue
        if f["min_makes"] and (h.makes or 0) < f["min_makes"]:
            continue
        if f["price"] == "free" and h.is_free is False:
            continue
        if f["price"] == "paid" and h.is_free is not False:
            continue
        if f["max_price"] is not None and h.price is not None and h.price > f["max_price"]:
            continue
        if cutoff and (h.published is None or h.published < cutoff):
            continue
        if f["license"] == "commercial" and h.commercial is False:
            continue
        if f["license"] == "commercial_strict" and h.commercial is not True:
            continue
        if f["has_image"] and not h.thumbnail:
            continue
        out.append(h)
    return out



def _dedupe_key(h: ModelHit) -> str:
    return " ".join(sorted(set(tokens(h.title))))


def dedupe(hits: list[ModelHit]) -> list[ModelHit]:
    groups: list[list[ModelHit]] = []
    index: dict[str, int] = {}
    for h in sorted(hits, key=lambda x: x.score, reverse=True):
        key = _dedupe_key(h)
        author = (h.author or "").lower()
        gi = index.get(key)
        if gi is not None:
            lead = groups[gi][0]
            same_author = not author or not lead.author or author == lead.author.lower()
            if lead.source != h.source and same_author and len(key) >= 6:
                groups[gi].append(h)
                continue
        index.setdefault(key, len(groups))
        groups.append([h])
    out = []
    for g in groups:
        lead = g[0]
        lead.also_on = [{"source": x.source, "url": x.url, "key": x.key} for x in g[1:]]
        out.append(lead)
    return out



STOP = set("""
a an the and or of for to in on with by from at as is it this that your my v1 v2 v3 v4 new
model models print printable printed printing print-in-place 3d stl 3mf obj file files free remix remixed
version ver mk mk2 mk3 set kit pack part parts easy simple small large big mini no support supports
и в на с для по из к от до или это под над без при для модель модели печать
""".split())


def facets(hits: list[ModelHit], query: str, limit: int = 10) -> list[dict[str, Any]]:
    n = len(hits)
    if n < 6:
        return []
    qset = set(tokens(query))
    docs: dict[str, set[int]] = defaultdict(set)
    for i, h in enumerate(hits):
        words = [w for w in tokens(h.title) if w not in STOP and not w.isdigit() and len(w) >= 3]
        for w in set(words):
            if w not in qset:
                docs[w].add(i)
        for a, b in zip(words, words[1:]):
            if a != b and not (a in qset and b in qset):
                docs[f"{a} {b}"].add(i)
        for tag in h.tags:
            t = tag.lower().strip()
            if t and t not in STOP and t not in qset and len(t) >= 3:
                docs[t].add(i)
    max_df = max(2, int(n * 0.6))
    cands = [(t, d) for t, d in docs.items() if 2 <= len(d) <= max_df]
    bigrams = {t: d for t, d in cands if " " in t}
    drop: set[str] = set()
    for bg, d in bigrams.items():
        for part in bg.split():
            pd = docs.get(part)
            if pd and len(d) >= 0.7 * len(pd):
                drop.add(part)
    cands = [(t, d) for t, d in cands if t not in drop]
    cands.sort(key=lambda x: (len(x[1]), " " in x[0], -len(x[0])), reverse=True)
    chosen: list[tuple[str, set[int]]] = []
    for t, d in cands:
        if any(len(d & cd) / len(d) > 0.75 for _, cd in chosen):
            continue
        chosen.append((t, d))
        if len(chosen) >= limit:
            break
    return [{"term": t, "count": len(d)} for t, d in chosen]



def rank(
    hits: list[ModelHit],
    query: str,
    weights: dict[str, float],
    source_weights: dict[str, float],
    half_life_days: float = 365,
    downloads_factor: float = 0.3,
    do_dedupe: bool = True,
    now: datetime | None = None,
    semantic: dict[str, float] | None = None,
    profile: Profile | None = None,
    alt_query: str | None = None,
) -> list[ModelHit]:
    now = now or datetime.now(timezone.utc)
    w = dict(weights)
    if semantic is None:
        w["semantic"] = 0
    if profile is None or (not profile.authors and not profile.tags):
        w["personal"] = 0
    w = normalize_weights(w)

    raw_pop = {id(h): _raw_popularity(h, downloads_factor) for h in hits}
    max_by_source: dict[str, float] = defaultdict(float)
    for h in hits:
        v = raw_pop[id(h)]
        if v is not None:
            max_by_source[h.source] = max(max_by_source[h.source], v)

    for h in hits:
        rp, mx = raw_pop[id(h)], max_by_source.get(h.source, 0.0)
        rel = relevance(query, h)
        if alt_query:
            rel = max(rel, relevance(alt_query, h))
        signals = {
            "relevance": rel,
            "semantic": (semantic or {}).get(h.key, NEUTRAL),
            "source_rank": source_rank(h),
            "popularity": (rp / mx) if (rp is not None and mx > 0) else NEUTRAL,
            "freshness": freshness(h, half_life_days, now),
            "personal": profile.score(h) if profile else 0.0,
        }
        sw = float(source_weights.get(h.source, 1.0))
        base = sum(w[k] * signals[k] for k in SIGNALS)
        h.score = round(base * sw, 4)
        h.breakdown = {k: round(signals[k], 3) for k in SIGNALS if w[k] > 0} | {"source_weight": sw}

    ranked = sorted(hits, key=lambda x: x.score, reverse=True)
    return dedupe(ranked) if do_dedupe else ranked


def sort_hits(hits: list[ModelHit], mode: str) -> list[ModelHit]:
    if mode == "likes":
        return sorted(hits, key=lambda h: h.likes or 0, reverse=True)
    if mode == "downloads":
        return sorted(hits, key=lambda h: h.downloads or 0, reverse=True)
    if mode == "makes":
        return sorted(hits, key=lambda h: h.makes or 0, reverse=True)
    if mode == "newest":
        epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
        return sorted(hits, key=lambda h: h.published or epoch, reverse=True)
    return hits
