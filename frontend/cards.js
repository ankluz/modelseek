import { $, SIGNALS, ago, api, el, fmt, srcInfo, state, toast } from "./util.js";
import { openFavPopover } from "./favorites.js";
import { openSimilarPopover } from "./imagesearch.js";

const SIGNAL_NAMES = Object.fromEntries(SIGNALS);

export function renderCard(h, opts = {}) {
  const node = $("#card-tpl").content.firstElementChild.cloneNode(true);
  const src = srcInfo(h.source);
  node.dataset.key = h.key;
  node.style.setProperty("--src", src.color);
  if (h.seen) node.classList.add("seen");

  const thumb = $(".thumb", node), img = $("img", node);
  thumb.href = h.url;
  if (h.thumbnail) {
    img.src = h.thumbnail;
    img.alt = h.title;
    img.addEventListener("error", () => { img.remove(); thumb.classList.add("noimg"); }, { once: true });
  } else { img.remove(); thumb.classList.add("noimg"); }

  const price = $(".price", node);
  if (h.is_free === true) { price.textContent = "FREE"; price.classList.add("free"); }
  else if (h.price) price.textContent = `${Number(h.price).toFixed(2)} ${h.currency || ""}`.trim();
  if (opts.isNew) $(".new-badge", node).hidden = false;

  $(".badge", node).textContent = src.label;
  const a = $(".title a", node);
  a.href = h.url;
  a.textContent = h.title;
  for (const link of [a, thumb]) link.addEventListener("click", () => markSeen(h, node));

  const authorBtn = $(".author-btn", node);
  if (h.author) {
    authorBtn.textContent = h.source === "searxng" ? h.author : "@" + h.author;
    if (opts.onHideAuthor && h.source !== "searxng") authorBtn.addEventListener("click", () => opts.onHideAuthor(h.author));
    else authorBtn.disabled = true;
  } else authorBtn.remove();

  const stats = [
    ["♥", fmt(h.likes), "лайки"],
    ["↓", fmt(h.downloads), "скачивания"],
    ["👁", h.downloads == null ? fmt(h.views) : null, "просмотры"],
    ["🖨", fmt(h.makes), "напечатали (makes)"],
    ["", ago(h.published), "опубликовано"],
  ];
  $(".stats", node).replaceChildren(...stats.filter(([, v]) => v).map(([icon, v, title]) =>
    el("li", { title }, icon ? `${icon} ${v}` : v)));
  if (h.license) {
    const cls = h.commercial === true ? "lic ok" : h.commercial === false ? "lic no" : "lic";
    $(".stats", node).append(el("li", { class: cls, title: "Лицензия: " + h.license }, shortLicense(h.license)));
  }

  $(".also", node).replaceChildren(...(h.also_on || []).map((o) => {
    const s = srcInfo(o.source);
    return el("a", { href: o.url, target: "_blank", rel: "noopener noreferrer", style: `--src:${s.color}` }, "+ " + s.label);
  }));

  const score = $(".score", node);
  if (h.score != null && h.breakdown && Object.keys(h.breakdown).length) {
    score.append(el("i", { style: `--p:${Math.min(100, (h.score / 1.5) * 100)}%` }), el("span", {}, h.score.toFixed(2)));
    const dl = $(".breakdown", node);
    for (const [k, v] of Object.entries(h.breakdown)) {
      dl.append(el("dt", {}, k === "source_weight" ? "× вес источника" : SIGNAL_NAMES[k] || k), el("dd", {}, Number(v).toFixed(2)));
    }
    score.addEventListener("click", () => { dl.hidden = !dl.hidden; });
  } else score.remove();

  const favBtn = $(".fav", node);
  const paintFav = () => {
    const coll = state.favorites.get(h.key);
    favBtn.textContent = coll ? "♥" : "♡";
    favBtn.classList.toggle("on", !!coll);
    favBtn.title = coll ? `В коллекции «${coll}»` : "В избранное";
  };
  paintFav();
  node.addEventListener("favchange", paintFav);
  favBtn.addEventListener("click", (e) => openFavPopover(h, favBtn, e));

  const simBtn = $(".similar", node);
  if (/^https?:\/\//.test(h.thumbnail || "")) simBtn.addEventListener("click", () => openSimilarPopover(h, simBtn));
  else simBtn.remove();

  const hideBtn = $(".hide", node);
  if (opts.onHide) hideBtn.addEventListener("click", () => opts.onHide(h, node));
  else hideBtn.remove();

  if (opts.favMode) setupFavExtra(h, node);
  return node;
}

function shortLicense(l) {
  const s = String(l);
  const m = s.match(/CC[\s-]*(0|BY[\w-]*)/i);
  if (m) return m[0].replace(/\s+/g, " ").toUpperCase();
  return s.length > 18 ? s.slice(0, 16) + "…" : s;
}

function markSeen(h, node) {
  node.classList.add("seen");
  fetch("/api/seen", { method: "POST", keepalive: true,
    headers: { "Content-Type": "application/json", "X-Modelseek": "1" }, body: JSON.stringify({ key: h.key }) })
    .catch(() => {});
}

function setupFavExtra(h, node) {
  const box = $(".fav-extra", node);
  box.hidden = false;
  const sel = $(".fav-coll", node);
  const names = new Set(state.collections.map((c) => c.name).concat(h.collection || "Избранное"));
  sel.replaceChildren(...[...names].sort().map((n) => el("option", { value: n, selected: n === h.collection }, n)));
  sel.addEventListener("change", async () => {
    await api("/api/favorites", { method: "PATCH", body: { key: h.key, collection: sel.value } });
    state.favorites.set(h.key, sel.value);
    document.dispatchEvent(new CustomEvent("favorites-changed"));
    toast(`Перемещено в «${sel.value}»`);
  });
  const note = $(".fav-note", node);
  note.value = h.note || "";
  let t;
  note.addEventListener("input", () => {
    clearTimeout(t);
    t = setTimeout(() => api("/api/favorites", { method: "PATCH", body: { key: h.key, note: note.value } }), 600);
  });
}

export function reconcile(grid, hits, make) {
  const existing = new Map([...grid.children].filter((n) => n.dataset.key).map((n) => [n.dataset.key, n]));
  const nodes = hits.map((h) => {
    const old = existing.get(h.key);
    if (old && old.dataset.sig === sig(h)) { old.classList.add("still"); return old; }
    const n = make(h);
    n.dataset.sig = sig(h);
    return n;
  });
  grid.replaceChildren(...nodes);
}
const sig = (h) => `${h.score}|${(h.also_on || []).length}|${h.seen}|${state.favorites.get(h.key) || ""}`;
