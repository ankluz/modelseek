import { $, $$, FILTER_KEYS, GUEST, SIGNALS, api, el, state, store, toast, words } from "./util.js";

let onChange = () => {};
export const setOnChange = (fn) => { onChange = fn; };

const EMPTY_FILTERS = { price: "any", max_price: null, min_likes: 0, min_downloads: 0, min_makes: 0, days: 0,
  license: "any", include: [], exclude: [], exclude_authors: [], has_image: false, hide_seen: false };

export function filtersFrom(f = {}) {
  const out = { ...EMPTY_FILTERS };
  for (const k of FILTER_KEYS) if (f[k] !== undefined && f[k] !== null) out[k] = f[k];
  if (f.free_only) out.price = "free";
  if (f.exclude_words) out.exclude = f.exclude_words;
  for (const k of ["include", "exclude", "exclude_authors"]) out[k] = words(out[k]);
  for (const k of ["min_likes", "min_downloads", "min_makes", "days"]) out[k] = Number(out[k]) || 0;
  out.max_price = out.max_price === "" || out.max_price == null ? null : Number(out.max_price);
  return out;
}

export function defaultSettings() {
  const d = state.config;
  return {
    weights: { ...d.weights },
    half_life: d.freshness_half_life_days,
    sourceWeights: { ...d.source_weights },
    enabled: state.sources.filter((s) => s.enabled && s.configured).map((s) => s.name),
    filters: filtersFrom(d.filters),
    dedupe: true,
    sort: "score",
    preset: null,
  };
}

export function loadSettings() {
  const saved = store.load();
  const base = defaultSettings();
  const s = saved ? { ...base, ...saved, weights: { ...base.weights, ...saved.weights },
    sourceWeights: { ...base.sourceWeights, ...saved.sourceWeights }, filters: filtersFrom(saved.filters) } : base;

  const sig = (x) => x.name + (x.configured ? ":ok" : ":nokey") + ":" + x.mode;
  const known = new Set(saved?.known || []);
  const enabled = new Set(s.enabled);
  for (const src of state.sources) if (!known.has(sig(src)) && src.enabled && src.configured) enabled.add(src.name);
  s.enabled = [...enabled].filter((n) => state.sources.some((x) => x.name === n && x.configured));
  s.known = state.sources.map(sig);
  state.settings = s;
  store.save(s);
}

export function save(rerun = true) {
  store.save(state.settings);
  paintActiveFilters();
  if (rerun) onChange();
}

const LABELS = {
  price: (v) => (v === "free" ? "бесплатно" : v === "paid" ? "платно" : null),
  max_price: (v) => (v != null ? `до ${v} €` : null),
  min_likes: (v) => (v ? `♥ от ${v}` : null),
  min_downloads: (v) => (v ? `↓ от ${v}` : null),
  min_makes: (v) => (v ? `🖨 от ${v}` : null),
  days: (v) => (v ? `за ${v} дн.` : null),
  license: (v) => (v === "commercial" ? "продажа не запрещена" : v === "commercial_strict" ? "можно продавать" : null),
  has_image: (v) => (v ? "с превью" : null),
  hide_seen: (v) => (v ? "без открытых" : null),
};

export function paintActiveFilters() {
  const f = state.settings.filters;
  const chips = [];
  for (const [k, fn] of Object.entries(LABELS)) {
    const label = fn(f[k]);
    if (label) chips.push(el("button", { type: "button", class: "fchip", title: "Убрать фильтр",
      onclick: () => { f[k] = EMPTY_FILTERS[k]; state.settings.preset = null; syncForm(); save(); } }, label, " ×"));
  }
  for (const w of f.include) chips.push(wordChip("include", w, "+ "));
  for (const w of f.exclude) chips.push(wordChip("exclude", w, "− "));
  for (const w of f.exclude_authors) chips.push(wordChip("exclude_authors", w, "без @"));
  $("#active-filters").replaceChildren(...chips);
  const sel = $("#preset");
  sel.value = state.settings.preset ?? "";
}

function wordChip(key, w, prefix) {
  const f = state.settings.filters;
  return el("button", { type: "button", class: "fchip " + key, title: "Убрать",
    onclick: () => { f[key] = f[key].filter((x) => x !== w); state.settings.preset = null; syncForm(); save(); } },
  prefix + w, " ×");
}

export function toggleWord(key, w) {
  const f = state.settings.filters;
  const other = key === "include" ? "exclude" : "include";
  f[other] = f[other].filter((x) => x !== w);
  f[key] = f[key].includes(w) ? f[key].filter((x) => x !== w) : [...f[key], w];
  state.settings.preset = null;
  syncForm();
  save();
}

export async function loadPresets() {
  state.presets = (await api("/api/presets")).items;
  paintPresets();
}

function applyPreset(p) {
  state.settings.filters = filtersFrom(p.filters);
  state.settings.preset = p.name;
  syncForm();
  save();
  toast(`Пресет «${p.name}»`);
}

function paintPresets() {
  const sel = $("#preset");
  sel.replaceChildren(el("option", { value: "" }, "Пресет: свои фильтры"),
    ...state.presets.map((p) => el("option", { value: p.name }, p.name)));
  sel.value = state.settings.preset ?? "";
  $("#preset-list").replaceChildren(...state.presets.map((p) => el("div", { class: "preset-row" },
    el("button", { type: "button", class: "chip-btn" + (p.name === state.settings.preset ? " active" : ""),
      onclick: () => { applyPreset(p); paintPresets(); } }, p.name),
    p.builtin ? null : el("button", { type: "button", class: "btn icon small", "aria-label": "Удалить пресет " + p.name,
      onclick: async () => { await api(`/api/presets/${p.id}`, { method: "DELETE" }); await loadPresets(); } }, "✕"))));
}

function slider({ id, label, value, min, max, step, color, disabled, hint, onInput }) {
  const out = el("output", { for: id });
  const input = el("input", { id, type: "range", min, max, step, disabled });
  input.value = value;
  const show = () => { out.textContent = Number(input.value).toFixed(2); };
  show();
  input.addEventListener("input", () => { show(); onInput(Number(input.value)); });
  return el("div", { class: "slider" + (disabled ? " off" : ""), style: color ? `--c:${color}` : null, title: hint || null },
    el("label", { for: id }, color ? el("span", { class: "dot" }) : null, el("span", {}, label)), out, input,
    hint ? el("small", { class: "muted" }, hint) : null);
}

export function syncForm() {
  const s = state.settings, f = s.filters;
  $$("input[name=price]").forEach((r) => { r.checked = r.value === f.price; });
  $("#f-max_price").value = f.max_price ?? "";
  $("#f-days").value = String(f.days || 0);
  $("#f-min_likes").value = f.min_likes || "";
  $("#f-min_downloads").value = f.min_downloads || "";
  $("#f-min_makes").value = f.min_makes || "";
  $("#f-license").value = f.license;
  $("#f-include").value = f.include.join(", ");
  $("#f-exclude").value = f.exclude.join(", ");
  $("#f-exclude_authors").value = f.exclude_authors.join(", ");
  $("#f-has_image").checked = !!f.has_image;
  $("#f-hide_seen").checked = !!f.hide_seen;
  $("#dedupe").checked = s.dedupe;
  $("#half_life").value = s.half_life;
  $("#sort").value = s.sort;

  const feat = state.config.features;
  $("#weights").replaceChildren(...SIGNALS.map(([k, label]) => {
    let disabled = false, hint = null;
    if (k === "semantic" && !feat.semantic.available) {
      disabled = true;
      hint = feat.semantic.error || "Выключено: semantic.enabled в config.yaml";
    }
    if (k === "personal" && GUEST) { disabled = true; hint = "Только у владельца: сервер не знает вашего избранного"; }
    else if (k === "personal" && !state.favorites.size) hint = "Заработает, когда в избранном что-то появится";
    return slider({ id: "w-" + k, label, value: s.weights[k] ?? 0, min: 0, max: 1, step: 0.05, disabled, hint,
      onInput: (v) => { s.weights[k] = v; save(); } });
  }));
  $("#source-weights").replaceChildren(...state.sources.filter((x) => !x.link_only).map((src) => slider({
    id: "sw-" + src.name, label: src.label, color: src.color, value: s.sourceWeights[src.name] ?? 1, min: 0, max: 2, step: 0.05,
    onInput: (v) => { s.sourceWeights[src.name] = v; save(); },
  })));
  paintActiveFilters();
}

function paintFeatures() {
  const f = state.config.features;
  const row = (name, ok, text) => el("li", { class: ok ? "ok" : "" }, el("b", {}, name), " — ", text);
  $("#features").replaceChildren(el("h4", {}, "Дополнительные функции"), el("ul", {},
    row("Смысловой поиск", f.semantic.available,
      f.semantic.available ? "включён" : (f.semantic.error || "выключен (semantic.enabled)")),
    row("Перевод запросов", f.translate.enabled && !f.translate.error,
      f.translate.enabled ? (f.translate.error ? "ошибка: " + f.translate.error : f.translate.model) : "выключен (translate.enabled)"),
    row("Склейка по картинке", f.image_dedupe.enabled,
      f.image_dedupe.enabled ? "включена" : f.image_dedupe.available ? "выключена (ranking.dedupe_images)" : "нужен Pillow"),
    row("Уведомления", f.notify.enabled, f.notify.enabled ? f.notify.kind : "не настроены (notify)"),
  ));
}

export async function paintHidden() {
  const items = (await api("/api/hidden")).items;
  $("#hidden-count").textContent = items.length ? `(${items.length})` : "";
  $("#hidden-items").replaceChildren(...items.map((h) => el("li", {},
    el("span", {}, h.title || h.key),
    el("button", { type: "button", class: "btn small ghost", onclick: async () => {
      await api("/api/hidden", { method: "DELETE", query: { key: h.key } });
      paintHidden();
      onChange();
    } }, "Вернуть"))));
}

export function bindSettings() {
  const s = () => state.settings;
  const f = () => state.settings.filters;
  const custom = () => { s().preset = null; };
  $$("input[name=price]").forEach((r) => r.addEventListener("change", () => { f().price = r.value; custom(); save(); }));
  const num = (id, key, allowNull = false) => $(id).addEventListener("input", (e) => {
    const v = e.target.value;
    f()[key] = v === "" ? (allowNull ? null : 0) : Number(v);
    custom(); save();
  });
  num("#f-max_price", "max_price", true);
  num("#f-min_likes", "min_likes");
  num("#f-min_downloads", "min_downloads");
  num("#f-min_makes", "min_makes");
  $("#f-days").addEventListener("change", (e) => { f().days = Number(e.target.value); custom(); save(); });
  $("#f-license").addEventListener("change", (e) => { f().license = e.target.value; custom(); save(); });
  for (const k of ["include", "exclude", "exclude_authors"]) {
    $("#f-" + k).addEventListener("change", (e) => { f()[k] = words(e.target.value); custom(); save(); });
  }
  $("#f-has_image").addEventListener("change", (e) => { f().has_image = e.target.checked; custom(); save(); });
  $("#f-hide_seen").addEventListener("change", (e) => { f().hide_seen = e.target.checked; custom(); save(); });
  $("#dedupe").addEventListener("change", (e) => { s().dedupe = e.target.checked; save(); });
  $("#half_life").addEventListener("input", (e) => { s().half_life = Number(e.target.value) || 365; save(); });
  $("#sort").addEventListener("change", (e) => { s().sort = e.target.value; save(); });

  $("#preset").addEventListener("change", (e) => {
    const p = state.presets.find((x) => x.name === e.target.value);
    if (p) { applyPreset(p); paintPresets(); }
  });
  $("#preset-save").addEventListener("submit", async (e) => {
    e.preventDefault();
    const name = $("#preset-name").value.trim();
    if (!name) return;
    await api("/api/presets", { method: "POST", body: { name, filters: f() } });
    s().preset = name;
    $("#preset-name").value = "";
    await loadPresets();
    save(false);
    toast(`Пресет «${name}» сохранён`);
  });

  $("#reset").addEventListener("click", () => {
    const enabled = s().enabled;
    state.settings = { ...defaultSettings(), known: s().known, enabled };
    syncForm(); paintPresets(); save();
  });
  $("#clear-cache").addEventListener("click", async () => { await api("/api/cache/clear", { method: "POST" }); toast("Кэш очищен"); onChange(); });
  $("#clear-history").addEventListener("click", async () => { await api("/api/history", { method: "DELETE" }); toast("История очищена"); });

  const drawer = $("#settings"), btn = $("#open-settings");
  const toggle = (open) => {
    drawer.hidden = !open;
    btn.setAttribute("aria-expanded", String(open));
    if (open) { paintHidden(); paintFeatures(); }
  };
  btn.addEventListener("click", () => toggle(drawer.hidden));
  $("#close-settings").addEventListener("click", () => toggle(false));
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !drawer.hidden) toggle(false); });
}
