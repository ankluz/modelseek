import { $, api, el, state, toast } from "./util.js";
import { renderCard } from "./cards.js";

export async function loadFavorites() {
  const data = await api("/api/favorites");
  state.favorites = new Map(data.items.map((f) => [f.key, f.collection]));
  state.collections = data.collections;
  paintCount();
  return data.items;
}

function paintCount() {
  const n = state.favorites.size;
  $("#fav-count").textContent = n ? String(n) : "";
}

function notifyCards(key) {
  document.querySelectorAll(`.card[data-key="${CSS.escape(key)}"]`).forEach((n) => n.dispatchEvent(new Event("favchange")));
  paintCount();
  document.dispatchEvent(new CustomEvent("favorites-changed"));
}

async function addTo(h, collection) {
  const hit = { ...h };
  delete hit.breakdown;
  const r = await api("/api/favorites", { method: "POST", body: { hit, collection } });
  state.favorites.set(h.key, collection);
  state.collections = r.collections;
  notifyCards(h.key);
  toast(`♥ Сохранено в «${collection}»`);
}

async function remove(h) {
  const r = await api("/api/favorites", { method: "DELETE", query: { key: h.key } });
  state.favorites.delete(h.key);
  state.collections = r.collections;
  notifyCards(h.key);
  toast("Убрано из избранного");
}

export function openFavPopover(h, anchor, ev) {
  const pop = $("#fav-pop");

  if (ev && (ev.ctrlKey || ev.metaKey) && !state.favorites.has(h.key)) { addTo(h, "Избранное"); return; }
  const current = state.favorites.get(h.key);
  const names = [...new Set(["Избранное", ...state.collections.map((c) => c.name)])];
  const input = el("input", { type: "text", placeholder: "Новая коллекция…", "aria-label": "Новая коллекция" });
  const close = () => { pop.hidden = true; document.removeEventListener("mousedown", outside, true); };
  const outside = (e) => { if (!pop.contains(e.target) && e.target !== anchor) close(); };

  pop.replaceChildren(
    el("div", { class: "pop-title" }, current ? `Сейчас в «${current}»` : "Сохранить в коллекцию"),
    ...names.map((n) => el("button", { type: "button", class: "pop-item" + (n === current ? " active" : ""),
      onclick: async () => { close(); if (n !== current) await addTo(h, n); } }, (n === current ? "✓ " : "") + n)),
    el("form", { class: "pop-new", onsubmit: async (e) => {
      e.preventDefault();
      const v = input.value.trim();
      if (v) { close(); await addTo(h, v); }
    } }, input, el("button", { type: "submit", class: "btn small" }, "+")),
    current ? el("button", { type: "button", class: "pop-item danger", onclick: async () => { close(); await remove(h); } }, "Убрать из избранного") : null,
  );
  const r = anchor.getBoundingClientRect();
  pop.hidden = false;
  const w = pop.offsetWidth, hgt = pop.offsetHeight;
  pop.style.left = Math.max(8, Math.min(window.innerWidth - w - 8, r.right - w)) + "px";
  pop.style.top = (r.bottom + hgt + 8 > window.innerHeight ? r.top - hgt - 6 : r.bottom + 6) + window.scrollY + "px";
  setTimeout(() => document.addEventListener("mousedown", outside, true));
  pop.onkeydown = (e) => { if (e.key === "Escape") close(); };
  (pop.querySelector(".pop-item") || input).focus();
}

let activeCollection = null;

export async function showFavorites() {
  const items = await loadFavorites();
  const chips = $("#collections");
  const all = [{ name: null, label: "Все", count: items.length },
    ...state.collections.map((c) => ({ name: c.name, label: c.name, count: c.count }))];
  if (activeCollection && !state.collections.some((c) => c.name === activeCollection)) activeCollection = null;
  chips.replaceChildren(...all.map((c) => el("button", {
    type: "button", class: "chip-btn" + (c.name === activeCollection ? " active" : ""),
    onclick: () => { activeCollection = c.name; showFavorites(); },
  }, c.label, el("span", { class: "n" }, String(c.count)))));

  const shown = activeCollection ? items.filter((f) => f.collection === activeCollection) : items;
  $("#fav-grid").replaceChildren(...shown.map((f) => renderCard({ ...f, score: null, breakdown: null }, { favMode: true })));
  $("#fav-empty").hidden = items.length > 0;
}
