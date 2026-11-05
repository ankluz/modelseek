// Внешний профиль: всё личное гостя живёт в его браузере, сервер ничего о нём не хранит.
// Настройки поиска и тема — в куки (cookie.js), избранное, скрытое, история и пресеты — в localStorage.

const KEY = "modelseek.guest.";
const LIMITS = { seen: 5000, history: 50, hidden: 2000, favorites: 2000, presets: 100 };

function read(name, fallback) {
  try { const v = JSON.parse(localStorage.getItem(KEY + name) || "null"); return v ?? fallback; } catch { return fallback; }
}
function write(name, value) {
  try { localStorage.setItem(KEY + name, JSON.stringify(value)); } catch {}
}
const now = () => new Date().toISOString();

function collections(favs) {
  const counts = new Map();
  for (const f of favs) counts.set(f.collection, (counts.get(f.collection) || 0) + 1);
  return [...counts].sort((a, b) => a[0].localeCompare(b[0])).map(([name, count]) => ({ name, count }));
}

function favorites(method, body, query) {
  let favs = read("favorites", []);
  if (method === "GET") {
    const c = query?.collection;
    return { items: c ? favs.filter((f) => f.collection === c) : favs, collections: collections(favs) };
  }
  if (method === "POST") {
    const hit = body?.hit;
    if (!hit || !hit.key || !/^https?:\/\//.test(hit.url || "")) throw new Error("нужен объект hit");
    const old = favs.find((f) => f.key === hit.key);
    favs = favs.filter((f) => f.key !== hit.key);
    favs.unshift({ ...hit, collection: body.collection || "Избранное", note: body.note ?? old?.note ?? "",
      added_at: old?.added_at || now() });
    favs = favs.slice(0, LIMITS.favorites);
  } else if (method === "PATCH") {
    const f = favs.find((x) => x.key === body?.key);
    if (f) {
      if (body.collection != null) f.collection = body.collection;
      if (body.note != null) f.note = body.note;
    }
  } else if (method === "DELETE") {
    const key = query?.key || body?.key;
    favs = favs.filter((f) => f.key !== key);
  }
  write("favorites", favs);
  return { ok: true, collections: collections(favs) };
}

function hidden(method, body, query) {
  let items = read("hidden", []);
  if (method === "GET") return { items };
  const key = query?.key || body?.key;
  items = items.filter((h) => h.key !== key);
  if (method === "POST" && key) items.unshift({ key, title: body?.title || "", added_at: now() });
  write("hidden", items.slice(0, LIMITS.hidden));
  return { ok: true };
}

export function markSeen(key) {
  if (!key) return;
  const seen = read("seen", []).filter((k) => k !== key);
  seen.push(key);
  write("seen", seen.slice(-LIMITS.seen));
}

function history(method, query) {
  let items = read("history", []);
  if (method === "GET") return { items };
  items = query?.query == null ? [] : items.filter((h) => h.query !== query.query);
  write("history", items);
  return { ok: true };
}

export function addHistory(query, results) {
  const q = String(query || "").trim();
  if (!q) return;
  const items = read("history", []).filter((h) => h.query.toLowerCase() !== q.toLowerCase());
  items.unshift({ query: q, results, ts: now() });
  write("history", items.slice(0, LIMITS.history));
}

async function presets(method, path, body, serverGet) {
  let own = read("presets", []);
  if (method === "GET") {
    const builtin = (await serverGet()).items || [];
    return { items: [...builtin, ...own.map((p) => ({ ...p, builtin: false }))] };
  }
  if (method === "DELETE") {
    const id = path.split("/").pop();
    own = own.filter((p) => p.id !== id);
  } else {
    const name = String(body?.name || "").trim();
    if (!name) throw new Error("нужно имя");
    own = own.filter((p) => p.name !== name);
    own.push({ id: "g" + Date.now().toString(36), name, filters: body.filters || {} });
    own.sort((a, b) => a.name.localeCompare(b.name));
  }
  write("presets", own.slice(0, LIMITS.presets));
  return { ok: true };
}

// Returns a promise for paths handled in the browser, or undefined to go to the server.
export function localApi(path, method, body, query, serverGet) {
  const run = (fn) => { try { return Promise.resolve(fn()); } catch (e) { return Promise.reject(e); } };
  if (path === "/api/favorites") return run(() => favorites(method, body, query));
  if (path === "/api/hidden") return run(() => hidden(method, body, query));
  if (path === "/api/seen") return run(() => { markSeen(body?.key); return { ok: true }; });
  if (path === "/api/history") return run(() => history(method, query));
  if (path.startsWith("/api/presets")) return presets(method, path, body, serverGet);
  if (path === "/api/subs") return run(() => ({ items: [], new_total: 0 }));
  if (path === "/api/cache/clear") return run(() => ({ ok: true }));
  return undefined;
}

// The server knows nothing about this visitor: drop what they hid, mark what they opened.
export function personalize(results, filters) {
  const hide = new Set(read("hidden", []).map((h) => h.key));
  const seen = new Set(read("seen", []));
  return results
    .filter((h) => !hide.has(h.key) && !(filters?.hide_seen && seen.has(h.key)))
    .map((h) => ({ ...h, seen: seen.has(h.key) }));
}

export function forgetAll() {
  for (const name of ["favorites", "hidden", "seen", "history", "presets"]) {
    try { localStorage.removeItem(KEY + name); } catch {}
  }
}
