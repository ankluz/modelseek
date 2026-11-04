from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .models import ModelHit

SCHEMA = """
CREATE TABLE IF NOT EXISTS favorites (
    key TEXT PRIMARY KEY, data TEXT NOT NULL, collection TEXT NOT NULL DEFAULT 'Избранное',
    note TEXT NOT NULL DEFAULT '', added_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS hidden (key TEXT PRIMARY KEY, title TEXT, added_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS seen (key TEXT PRIMARY KEY, ts TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS history (
    query TEXT PRIMARY KEY, ts TEXT NOT NULL, times INTEGER NOT NULL DEFAULT 1, results INTEGER);
CREATE TABLE IF NOT EXISTS presets (
    id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE NOT NULL, filters TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS subs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, query TEXT NOT NULL, params TEXT NOT NULL,
    interval_min INTEGER NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
    last_run TEXT, next_run TEXT NOT NULL, last_error TEXT, created TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sub_known (sub_id INTEGER NOT NULL, key TEXT NOT NULL, PRIMARY KEY (sub_id, key));
CREATE TABLE IF NOT EXISTS sub_items (
    sub_id INTEGER NOT NULL, key TEXT NOT NULL, data TEXT NOT NULL, first_seen TEXT NOT NULL,
    is_new INTEGER NOT NULL DEFAULT 1, PRIMARY KEY (sub_id, key));
"""

BUILTIN_PRESETS: list[dict[str, Any]] = [
    {"name": "Без фильтров", "filters": {}},
    {"name": "Проверенные", "filters": {"min_likes": 50, "min_makes": 3, "has_image": True}},
    {"name": "Свежие (30 дней)", "filters": {"days": 30}},
    {"name": "Бесплатные и популярные", "filters": {"price": "free", "min_likes": 100}},
    {"name": "Можно продавать печать", "filters": {"license": "commercial_strict"}},
    {"name": "Без мусора", "filters": {"has_image": True, "min_likes": 5, "exclude": ["test", "calibration"]}},
    {"name": "Новое для меня", "filters": {"hide_seen": True}},
]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Storage:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._db.executescript(SCHEMA)
            self._db.commit()

    def _q(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            cur = self._db.execute(sql, args)
            rows = cur.fetchall()
            self._db.commit()
            return rows

    def _x(self, sql: str, args: tuple = ()) -> int:
        with self._lock:
            cur = self._db.execute(sql, args)
            self._db.commit()
            return cur.lastrowid or cur.rowcount

    def add_favorite(self, hit: dict[str, Any], collection: str = "Избранное", note: str = "") -> None:
        key = hit.get("key") or f"{hit['source']}:{hit['id']}"
        data = {k: v for k, v in hit.items() if k not in ("score", "breakdown")}
        self._x("""INSERT INTO favorites(key, data, collection, note, added_at) VALUES(?,?,?,?,?)
                   ON CONFLICT(key) DO UPDATE SET data=excluded.data, collection=excluded.collection""",
                (key, json.dumps(data, ensure_ascii=False), collection or "Избранное", note, now_iso()))

    def update_favorite(self, key: str, collection: str | None = None, note: str | None = None) -> None:
        if collection is not None:
            self._x("UPDATE favorites SET collection=? WHERE key=?", (collection or "Избранное", key))
        if note is not None:
            self._x("UPDATE favorites SET note=? WHERE key=?", (note, key))

    def remove_favorite(self, key: str) -> None:
        self._x("DELETE FROM favorites WHERE key=?", (key,))

    def favorites(self, collection: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM favorites" + (" WHERE collection=?" if collection else "") + " ORDER BY added_at DESC"
        rows = self._q(sql, (collection,) if collection else ())
        out = []
        for r in rows:
            d = json.loads(r["data"])
            d.update(key=r["key"], collection=r["collection"], note=r["note"], added_at=r["added_at"])
            out.append(d)
        return out

    def favorite_keys(self) -> dict[str, str]:
        return {r["key"]: r["collection"] for r in self._q("SELECT key, collection FROM favorites")}

    def collections(self) -> list[dict[str, Any]]:
        rows = self._q("SELECT collection, COUNT(*) n FROM favorites GROUP BY collection ORDER BY collection")
        return [{"name": r["collection"], "count": r["n"]} for r in rows]

    def favorite_hits(self) -> list[ModelHit]:
        return [ModelHit.from_dict(d) for d in self.favorites()]

    def hide(self, key: str, title: str = "") -> None:
        self._x("INSERT OR REPLACE INTO hidden(key, title, added_at) VALUES(?,?,?)", (key, title, now_iso()))

    def unhide(self, key: str) -> None:
        self._x("DELETE FROM hidden WHERE key=?", (key,))

    def hidden(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self._q("SELECT * FROM hidden ORDER BY added_at DESC")]

    def hidden_keys(self) -> set[str]:
        return {r["key"] for r in self._q("SELECT key FROM hidden")}

    def mark_seen(self, key: str) -> None:
        self._x("INSERT OR REPLACE INTO seen(key, ts) VALUES(?,?)", (key, now_iso()))

    def seen_keys(self) -> set[str]:
        return {r["key"] for r in self._q("SELECT key FROM seen")}

    def add_history(self, query: str, results: int) -> None:
        q = query.strip()
        if not q:
            return
        self._x("""INSERT INTO history(query, ts, times, results) VALUES(?,?,1,?)
                   ON CONFLICT(query) DO UPDATE SET ts=excluded.ts, times=times+1, results=excluded.results""",
                (q, now_iso(), results))

    def history(self, limit: int = 50) -> list[dict[str, Any]]:
        return [dict(r) for r in self._q("SELECT * FROM history ORDER BY ts DESC LIMIT ?", (limit,))]

    def delete_history(self, query: str | None = None) -> None:
        if query is None:
            self._x("DELETE FROM history")
        else:
            self._x("DELETE FROM history WHERE query=?", (query,))

    def presets(self) -> list[dict[str, Any]]:
        own = [{"id": r["id"], "name": r["name"], "filters": json.loads(r["filters"]), "builtin": False}
               for r in self._q("SELECT * FROM presets ORDER BY name")]
        return [{"id": None, **p, "builtin": True} for p in BUILTIN_PRESETS] + own

    def save_preset(self, name: str, filters: dict[str, Any]) -> None:
        self._x("""INSERT INTO presets(name, filters) VALUES(?,?)
                   ON CONFLICT(name) DO UPDATE SET filters=excluded.filters""",
                (name.strip(), json.dumps(filters, ensure_ascii=False)))

    def delete_preset(self, pid: int) -> None:
        self._x("DELETE FROM presets WHERE id=?", (pid,))

    def add_sub(self, query: str, params: dict[str, Any], interval_min: int) -> int:
        now = datetime.now(timezone.utc)
        return self._x("""INSERT INTO subs(query, params, interval_min, enabled, next_run, created)
                          VALUES(?,?,?,1,?,?)""",
                       (query, json.dumps(params, ensure_ascii=False), max(15, int(interval_min)),
                        now.isoformat(timespec="seconds"), now.isoformat(timespec="seconds")))

    def update_sub(self, sid: int, interval_min: int | None = None, enabled: bool | None = None,
                   params: dict[str, Any] | None = None) -> None:
        if interval_min is not None:
            self._x("UPDATE subs SET interval_min=? WHERE id=?", (max(15, int(interval_min)), sid))
        if enabled is not None:
            self._x("UPDATE subs SET enabled=? WHERE id=?", (1 if enabled else 0, sid))
            if enabled:
                self._x("UPDATE subs SET next_run=? WHERE id=?", (now_iso(), sid))
        if params is not None:
            self._x("UPDATE subs SET params=? WHERE id=?", (json.dumps(params, ensure_ascii=False), sid))

    def delete_sub(self, sid: int) -> None:
        self._x("DELETE FROM sub_items WHERE sub_id=?", (sid,))
        self._x("DELETE FROM sub_known WHERE sub_id=?", (sid,))
        self._x("DELETE FROM subs WHERE id=?", (sid,))

    def _sub_row(self, r: sqlite3.Row) -> dict[str, Any]:
        d = dict(r)
        d["params"] = json.loads(d["params"])
        d["enabled"] = bool(d["enabled"])
        counts = self._q("SELECT COUNT(*) total, SUM(is_new) new FROM sub_items WHERE sub_id=?", (d["id"],))[0]
        d["total"] = counts["total"] or 0
        d["new"] = counts["new"] or 0
        return d

    def subs(self) -> list[dict[str, Any]]:
        return [self._sub_row(r) for r in self._q("SELECT * FROM subs ORDER BY created DESC")]

    def sub(self, sid: int) -> dict[str, Any] | None:
        rows = self._q("SELECT * FROM subs WHERE id=?", (sid,))
        return self._sub_row(rows[0]) if rows else None

    def due_subs(self) -> list[dict[str, Any]]:
        return [self._sub_row(r) for r in
                self._q("SELECT * FROM subs WHERE enabled=1 AND next_run<=? ORDER BY next_run", (now_iso(),))]

    def mark_sub_run(self, sid: int, interval_min: int, error: str | None = None) -> None:
        now = datetime.now(timezone.utc)
        nxt = now + timedelta(minutes=interval_min)
        self._x("UPDATE subs SET last_run=?, next_run=?, last_error=? WHERE id=?",
                (now.isoformat(timespec="seconds"), nxt.isoformat(timespec="seconds"), error, sid))

    def record_sub_items(self, sid: int, hits: list[dict[str, Any]], first_run: bool, keep: int = 200) -> int:
        known = {r["key"] for r in self._q("SELECT key FROM sub_items WHERE sub_id=?", (sid,))}
        known |= {r["key"] for r in self._q("SELECT key FROM sub_known WHERE sub_id=?", (sid,))}
        new = 0
        now = now_iso()
        with self._lock:
            for h in hits:
                key = h.get("key")
                group = [key] + [a.get("key") for a in h.get("also_on") or [] if a.get("key")]
                self._db.executemany("INSERT OR IGNORE INTO sub_known(sub_id, key) VALUES(?,?)",
                                     [(sid, k) for k in group if k])
                if not key or any(k in known for k in group):
                    continue
                data = {k: v for k, v in h.items() if k not in ("breakdown",)}
                self._db.execute("INSERT INTO sub_items(sub_id, key, data, first_seen, is_new) VALUES(?,?,?,?,?)",
                                 (sid, key, json.dumps(data, ensure_ascii=False), now, 0 if first_run else 1))
                new += 0 if first_run else 1
            self._db.execute("""DELETE FROM sub_items WHERE sub_id=? AND is_new=0 AND key NOT IN (
                SELECT key FROM sub_items WHERE sub_id=? ORDER BY first_seen DESC LIMIT ?)""", (sid, sid, keep))
            self._db.commit()
        return new

    def sub_items(self, sid: int, only_new: bool = False) -> list[dict[str, Any]]:
        sql = "SELECT * FROM sub_items WHERE sub_id=?" + (" AND is_new=1" if only_new else "") + \
              " ORDER BY is_new DESC, first_seen DESC"
        out = []
        for r in self._q(sql, (sid,)):
            d = json.loads(r["data"])
            d.update(key=r["key"], first_seen=r["first_seen"], is_new=bool(r["is_new"]))
            out.append(d)
        return out

    def mark_sub_seen(self, sid: int) -> None:
        self._x("UPDATE sub_items SET is_new=0 WHERE sub_id=?", (sid,))

    def total_new(self) -> int:
        r = self._q("SELECT COUNT(*) n FROM sub_items s JOIN subs ON subs.id=s.sub_id WHERE s.is_new=1")
        return r[0]["n"] or 0

    def close(self):
        with self._lock:
            self._db.close()
