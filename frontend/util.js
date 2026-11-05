import { localApi, markSeen } from "./guest.js";

export const $ = (s, el = document) => el.querySelector(s);
export const $$ = (s, el = document) => [...el.querySelectorAll(s)];

export const SIGNALS = [
  ["relevance", "Совпадение слов"],
  ["semantic", "Смысловая близость"],
  ["source_rank", "Позиция на сайте"],
  ["popularity", "Популярность"],
  ["freshness", "Свежесть"],
  ["personal", "Похоже на избранное"],
];
export const FILTER_KEYS = ["price", "max_price", "min_likes", "min_downloads", "min_makes", "days", "license",
  "include", "exclude", "exclude_authors", "has_image", "hide_seen"];

export const state = {
  sources: [],
  config: null,
  settings: null,
  presets: [],
  favorites: new Map(),
  collections: [],
};

// "admin" — this computer, everything is on the server; "public" — a visitor from outside, see guest.js.
export const PROFILE = document.documentElement.dataset.profile === "public" ? "public" : "admin";
export const GUEST = PROFILE === "public";

async function serverApi(path, { method = "GET", body, query } = {}) {
  const url = path + (query ? "?" + new URLSearchParams(query) : "");
  const opts = { method, headers: {} };
  if (method !== "GET") opts.headers["X-Modelseek"] = "1";
  if (body !== undefined) { opts.headers["Content-Type"] = "application/json"; opts.body = JSON.stringify(body); }
  const r = await fetch(url, opts);
  let data = null;
  try { data = await r.json(); } catch {}
  if (r.status === 401 && data && data.login) document.dispatchEvent(new CustomEvent("need-login"));
  if (!r.ok) throw new Error((data && data.error) || r.statusText);
  return data;
}

export function api(path, opts = {}) {
  if (GUEST) {
    const local = localApi(path, opts.method || "GET", opts.body, opts.query, () => serverApi(path));
    if (local) return local;
  }
  return serverApi(path, opts);
}

export function markSeenKey(key) {
  if (GUEST) { markSeen(key); return; }
  fetch("/api/seen", { method: "POST", keepalive: true,
    headers: { "Content-Type": "application/json", "X-Modelseek": "1" }, body: JSON.stringify({ key }) })
    .catch(() => {});
}

// Cookies are shared by every port of a host, so the guest's ones carry their own names; the admin profile has none.
export const cookie = {
  get(name) {
    const m = document.cookie.split("; ").find((c) => c.startsWith(name + "="));
    if (!m) return null;
    try { return JSON.parse(decodeURIComponent(m.slice(name.length + 1))); } catch { return null; }
  },
  set(name, value) {
    const v = encodeURIComponent(JSON.stringify(value));
    if (v.length > 3800) return false;
    const secure = location.protocol === "https:" ? "; Secure" : "";
    document.cookie = `${name}=${v}; Path=/; Max-Age=31536000; SameSite=Strict${secure}`;
    return true;
  },
  remove(name) { document.cookie = `${name}=; Path=/; Max-Age=0; SameSite=Strict`; },
};
export const GUEST_SETTINGS = "ms_guest_settings";
export const GUEST_UI = "ms_guest_ui";
// The guest's own site keys; the server reads them with each search and keeps nothing.
export const GUEST_KEYS = "ms_guest_keys";

const STORE_KEY = "modelseek.settings.v2";
const localStore = {
  load() { try { return JSON.parse(localStorage.getItem(STORE_KEY) || "null"); } catch { return null; } },
  save(v) { try { localStorage.setItem(STORE_KEY, JSON.stringify(v)); } catch {} },
};
// A guest's settings go to a cookie; if they ever outgrow it, the browser's storage takes over.
const cookieStore = {
  load() { return cookie.get(GUEST_SETTINGS) || localStore.load(); },
  save(v) { if (cookie.set(GUEST_SETTINGS, v)) { try { localStorage.removeItem(STORE_KEY); } catch {} } else localStore.save(v); },
};
export const store = GUEST ? cookieStore : localStore;

export const uiStore = {
  load() {
    if (GUEST) return cookie.get(GUEST_UI) || {};
    try { return JSON.parse(localStorage.getItem("modelseek.ui") || "{}"); } catch { return {}; }
  },
  save(v) {
    if (GUEST) { cookie.set(GUEST_UI, v); return; }
    try { localStorage.setItem("modelseek.ui", JSON.stringify(v)); } catch {}
  },
};

export const debounce = (fn, ms) => { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; };

export const fmt = (n) => n == null ? null
  : n >= 1e6 ? (n / 1e6).toFixed(1).replace(/\.0$/, "") + "M"
  : n >= 1e3 ? (n / 1e3).toFixed(1).replace(/\.0$/, "") + "k" : String(n);

const rtf = new Intl.RelativeTimeFormat("ru", { numeric: "auto" });
export function ago(iso) {
  if (!iso) return null;
  const days = (Date.now() - new Date(iso).getTime()) / 864e5;
  if (days < 0.04) return "только что";
  if (days < 1) return rtf.format(-Math.round(days * 24), "hour");
  if (days < 30) return rtf.format(-Math.round(days), "day");
  if (days < 365) return rtf.format(-Math.round(days / 30), "month");
  return rtf.format(-Math.round(days / 365), "year");
}
export function when(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  return d.toLocaleString("ru", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
}

export const srcInfo = (name) => state.sources.find((s) => s.name === name) || { label: name, color: "#888" };

let toastTimer;
export function toast(text, ms = 2600) {
  const t = $("#toast");
  t.textContent = text;
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.hidden = true; }, ms);
}

export function el(tag, attrs = {}, ...children) {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v == null || v === false) continue;
    if (k === "class") n.className = v;
    else if (k === "style") n.style.cssText = v;
    else if (k.startsWith("on")) n.addEventListener(k.slice(2), v);
    else if (k in n && typeof v !== "string") n[k] = v;
    else n.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat()) if (c != null && c !== false) n.append(c);
  return n;
}

export const words = (s) => (Array.isArray(s) ? s : String(s || "").split(","))
  .map((w) => String(w).trim()).filter(Boolean);
