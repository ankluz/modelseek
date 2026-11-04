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

export async function api(path, { method = "GET", body, query } = {}) {
  const url = path + (query ? "?" + new URLSearchParams(query) : "");
  const opts = { method, headers: {} };
  if (method !== "GET") opts.headers["X-Modelseek"] = "1";
  if (body !== undefined) { opts.headers["Content-Type"] = "application/json"; opts.body = JSON.stringify(body); }
  const r = await fetch(url, opts);
  let data = null;
  try { data = await r.json(); } catch {}
  if (!r.ok) throw new Error((data && data.error) || r.statusText);
  return data;
}

const STORE_KEY = "modelseek.settings.v2";
export const store = {
  load() { try { return JSON.parse(localStorage.getItem(STORE_KEY) || "null"); } catch { return null; } },
  save(v) { try { localStorage.setItem(STORE_KEY, JSON.stringify(v)); } catch {} },
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
