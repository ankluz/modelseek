import { $, $$, api, el, toast } from "./util.js";

let S = null;
let extras = null;
let onSaved = async () => {};
export const setOnSettingsSaved = (fn) => { onSaved = fn; };

export const THEMES = [
  { id: "auto", name: "Как в системе", note: "Плита или Latte", sw: ["#16171a", "#d9a441", "#eff1f5", "#8839ef"] },
  { id: "plate", name: "Плита", note: "графит и золотой PEI", sw: ["#16171a", "#222429", "#d9a441", "#ece8df"] },
  { id: "mocha", name: "Catppuccin Mocha", note: "тёмная", sw: ["#1e1e2e", "#262637", "#cba6f7", "#cdd6f4"] },
  { id: "latte", name: "Catppuccin Latte", note: "светлая", sw: ["#eff1f5", "#ffffff", "#8839ef", "#4c4f69"] },
  { id: "nord", name: "Nord", note: "тёмная", sw: ["#2e3440", "#353c4a", "#88c0d0", "#eceff4"] },
  { id: "dracula", name: "Dracula", note: "тёмная", sw: ["#282a36", "#2f3241", "#ff79c6", "#f8f8f2"] },
  { id: "gruvbox", name: "Gruvbox", note: "тёмная", sw: ["#282828", "#3c3836", "#fe8019", "#ebdbb2"] },
  { id: "tokyo", name: "Tokyo Night", note: "тёмная", sw: ["#1a1b26", "#1f2335", "#7aa2f7", "#c0caf5"] },
  { id: "solarized", name: "Solarized Light", note: "светлая", sw: ["#fdf6e3", "#fffbf0", "#cb4b16", "#45575e"] },
];
const UI_KEY = "modelseek.ui";
export function ui() { try { return JSON.parse(localStorage.getItem(UI_KEY) || "{}"); } catch { return {}; } }
function saveUi(v) { try { localStorage.setItem(UI_KEY, JSON.stringify(v)); } catch {} }
export function applyUi() {
  const u = ui();
  let t = u.theme || "auto";
  if (t === "auto") t = matchMedia("(prefers-color-scheme: light)").matches ? "latte" : "plate";
  document.documentElement.dataset.theme = t;
  document.documentElement.dataset.density = u.density || "normal";
}
matchMedia("(prefers-color-scheme: light)").addEventListener?.("change", applyUi);

const field = (label, input, hint) => el("label", { class: "sfield" }, el("span", { class: "sl" }, label), input,
  hint ? el("small", { class: "muted" }, hint) : null);
const text = (value, attrs = {}) => el("input", { type: "text", value: value ?? "", ...attrs });
const num = (value, attrs = {}) => el("input", { type: "number", value: value ?? "", ...attrs });
const check = (checked, label, attrs = {}) => {
  const i = el("input", { type: "checkbox", checked: !!checked, ...attrs });
  return { input: i, node: el("label", { class: "check" }, i, " ", label) };
};
const select = (value, options) => el("select", {},
  ...options.map(([v, l]) => el("option", { value: v, selected: String(v) === String(value) }, l)));
const panel = (title, desc, ...children) => el("section", { class: "spanel" },
  el("h2", {}, title), desc ? el("p", { class: "muted lead" }, desc) : null, ...children);
const saveRow = (label, onClick, extra = []) => {
  const btn = el("button", { type: "button", class: "btn primary" }, label);
  btn.addEventListener("click", async () => { btn.disabled = true; try { await onClick(); } finally { btn.disabled = false; } });
  return el("div", { class: "save-row" }, ...extra, btn);
};
const pill = (ok, yes, no) => el("span", { class: "pill " + (ok ? "ok" : "no") }, ok ? yes : no);

async function patch(p, msg = "Сохранено") {
  try {
    S = await api("/api/settings", { method: "PATCH", body: { patch: p } });
    toast(msg);
    await onSaved();
    return true;
  } catch (e) { toast("Не сохранилось: " + e.message, 7000); return false; }
}

async function saveSecret(name, value) {
  try {
    S = await api("/api/secrets", { method: "PUT", body: { values: { [name]: value } } });
    toast(value ? `Ключ ${name} сохранён` : `Ключ ${name} удалён`);
    await onSaved();
    return true;
  } catch (e) { toast("Не сохранилось: " + e.message, 6000); return false; }
}

function secretField(envName, label, help) {
  const set = !!S.secrets[envName];
  const input = el("input", { type: "password", autocomplete: "off", placeholder: set ? "•••••• (задан)" : "не задан" });
  const save = el("button", { type: "button", class: "btn small" }, "Сохранить");
  save.addEventListener("click", async () => { if (await saveSecret(envName, input.value)) rerender(); });
  const del = set ? el("button", { type: "button", class: "btn small ghost danger" }, "Удалить") : null;
  del?.addEventListener("click", async () => { if (await saveSecret(envName, "")) rerender(); });
  return el("div", { class: "sfield" },
    el("span", { class: "sl" }, label, " ", pill(set, "задан", "не задан")),
    el("div", { class: "inline" }, input, save, del),
    el("small", { class: "muted" }, `Переменная ${envName}. `, help || ""));
}

function jobConsole() {
  const bar = el("div", { class: "jbar" }, el("i"));
  const pre = el("pre", { class: "jlog", "aria-live": "polite" });
  const status = el("div", { class: "jstatus" });
  const box = el("div", { class: "job", hidden: true }, status, bar, pre);
  box.run = async (startPromise) => {
    box.hidden = false;
    pre.textContent = "";
    status.textContent = "Запускаю…";
    status.className = "jstatus";
    let job;
    try { job = (await startPromise).job; } catch (e) { status.textContent = "Ошибка: " + e.message; status.className = "jstatus err"; return "error"; }
    let since = 0;
    status.textContent = job.title + "…";
    for (;;) {
      const j = await api(`/api/jobs/${job.id}`, { query: { since } }).catch(() => null);
      if (j) {
        if (j.log.length) {

          pre.textContent = pre.textContent.split("\n").slice(0, since).concat(j.log).join("\n");
          since = Math.max(0, j.log_total - 1);
          pre.scrollTop = pre.scrollHeight;
        }
        bar.hidden = j.progress == null;
        bar.firstChild.style.width = ((j.progress || 0) * 100) + "%";
        if (j.status !== "running") {
          status.textContent = j.status === "ok" ? "Готово" : "Не получилось — подробности в логе";
          status.className = "jstatus " + (j.status === "ok" ? "ok" : "err");
          return j.status;
        }
      }
      await new Promise((r) => setTimeout(r, 700));
    }
  };
  return box;
}

const KEY_HELP = {
  thingiverse: ["https://www.thingiverse.com/apps/create", "Создайте приложение типа Desktop app и скопируйте App Token."],
  cults3d: ["https://cults3d.com/en/api/keys", "Логин — ваш ник на Cults, ключ — со страницы API keys."],
  myminifactory: ["https://www.myminifactory.com/settings/developer", "Ключ API в настройках разработчика."],
};
const KEY_LABEL = { token_env: "Токен", key_env: "API-ключ", user_env: "Логин" };

function sectionLook() {
  const u = ui();
  const cards = THEMES.map((t) => {
    const b = el("button", { type: "button", class: "theme-card" + ((u.theme || "auto") === t.id ? " active" : ""),
      "aria-pressed": String((u.theme || "auto") === t.id) },
    el("span", { class: "swatches" }, ...t.sw.map((c) => el("i", { style: `background:${c}` }))),
    el("b", {}, t.name), el("small", { class: "muted" }, t.note));
    b.addEventListener("click", () => { saveUi({ ...ui(), theme: t.id }); applyUi(); rerender(); });
    return b;
  });
  const dens = select(u.density || "normal", [["compact", "Компактные"], ["normal", "Обычные"], ["large", "Крупные"]]);
  dens.addEventListener("change", () => { saveUi({ ...ui(), density: dens.value }); applyUi(); toast("Размер карточек: " + dens.selectedOptions[0].text); });
  return [
    panel("Тема", "Хранится в этом браузере.", el("div", { class: "theme-grid" }, ...cards)),
    panel("Карточки", null, field("Размер карточек в выдаче", dens)),
  ];
}

function sectionSources() {
  const cfg = S.config.sources || {};
  const ex = Object.fromEntries((extras?.items || []).map((i) => [i.key, i]));
  return S.builtin.map((b) => {
    const c = cfg[b.name] || { enabled: false };
    const en = check(c.enabled !== false && cfg[b.name] !== undefined, "Включён");
    const mode = select(c.mode || b.default_mode, b.allowed_modes.map((m) => [m, m]));
    const modeHint = el("small", { class: "muted" });
    const showHint = () => {
      const m = mode.value;
      let need = "";
      if (m === "browser" && !(ex.playwright?.installed && ex.playwright?.chromium)) need = " Нужен компонент Playwright + Chromium (раздел «Компоненты»).";
      if (m === "impersonate" && !ex.curl_cffi?.installed) need = " Нужен компонент curl_cffi (раздел «Компоненты»).";
      modeHint.textContent = (S.modes[m] || "") + need;
      modeHint.classList.toggle("warn", !!need);
    };
    mode.addEventListener("change", showHint);
    showHint();
    const weight = num(c.weight ?? 1, { min: 0, max: 10, step: 0.05 });
    const extra = [];
    let sx = null;
    if (b.name === "searxng") {
      sx = { url: text(c.url || "http://127.0.0.1:8089"), sites: el("textarea", { rows: 4 }) };
      sx.sites.value = (c.sites || []).join("\n");
      extra.push(field("Адрес SearXNG", sx.url, "Свой экземпляр с включённым форматом json. В Docker: http://searxng:8080"),
        field("Сайты (по одному в строке)", sx.sites, "Поиск идёт с фильтром site: по этим доменам."));
    }
    const secrets = b.secret_keys.map((k) => {
      const envName = c[k] || `${b.name.toUpperCase()}_${k.replace("_env", "").toUpperCase()}`;
      const [href, how] = KEY_HELP[b.name] || [];
      return secretField(envName, KEY_LABEL[k] || k, how ? el("span", {}, how, " ", el("a", { href, target: "_blank", rel: "noopener" }, "Получить ключ ↗")) : "");
    });
    return panel(b.label, b.official_api ? "Официальный API." : "Внутренний API сайта: может измениться без предупреждения.",
      el("div", { class: "row-fields" }, en.node, field("Режим", mode), field("Вес в рейтинге", weight)),
      modeHint, ...extra, ...secrets,
      saveRow("Сохранить", async () => {
        const v = { enabled: en.input.checked, mode: mode.value, weight: Number(weight.value) || 0 };
        if (sx) { v.url = sx.url.value.trim(); v.sites = sx.sites.value.split(/\s+/).filter(Boolean); }
        await patch({ sources: { [b.name]: v } }, `${b.label}: сохранено`);
      }));
  });
}

const FIELD_NAMES = [["id", "ID"], ["title", "Название"], ["url", "Ссылка (путь или шаблон {id})"], ["thumbnail", "Превью"],
  ["author", "Автор"], ["likes", "Лайки"], ["downloads", "Скачивания"], ["makes", "Печати (makes)"],
  ["published", "Дата"], ["price", "Цена"], ["tags", "Теги"], ["license", "Лицензия"]];

function customEditor(name, c, onClose) {
  const isNew = !name;
  const f = {
    name: text(name || "", { placeholder: "mysite", pattern: "[a-z0-9][a-z0-9_-]*", disabled: !isNew }),
    label: text(c.label || "", { placeholder: "Мой сайт" }),
    color: el("input", { type: "color", value: c.color || "#8a8f98" }),
    mode: select(c.mode || "http", [["http", "http"], ["impersonate", "impersonate"], ["browser", "browser"], ["link", "link — только ссылка"]]),
    weight: num(c.weight ?? 1, { min: 0, max: 10, step: 0.05 }),
    method: select(c.method || "GET", [["GET", "GET"], ["POST", "POST"]]),
    url: text(c.url || "", { placeholder: "https://api.site.com/search?q={q}&limit={limit}&offset={offset}" }),
    body: el("textarea", { rows: 3, placeholder: '{"query": "{q}", "size": "{limit}"}' }),
    headers: el("textarea", { rows: 2, placeholder: '{"Accept": "application/json"}' }),
    items_path: text(c.items_path || "", { placeholder: "data.items" }),
    url_prefix: text(c.url_prefix || "", { placeholder: "https://site.com" }),
    search_page: text(c.search_page || "", { placeholder: "https://site.com/search?q={q}" }),
  };
  f.body.value = typeof c.body === "string" ? c.body : c.body ? JSON.stringify(c.body) : "";
  f.headers.value = c.headers && Object.keys(c.headers).length ? JSON.stringify(c.headers) : "";
  const fields = Object.fromEntries(FIELD_NAMES.map(([k]) => [k, text((c.fields || {})[k] || "", { placeholder: "авто" })]));
  const bodyWrap = field("Тело запроса (JSON, для POST)", f.body);
  const apiBox = el("div", {});
  const syncVis = () => { bodyWrap.hidden = f.method.value !== "POST"; apiBox.hidden = f.mode.value === "link"; };
  f.method.addEventListener("change", syncVis);
  f.mode.addEventListener("change", syncVis);

  const collect = () => {
    let headers = {};
    if (f.headers.value.trim()) headers = JSON.parse(f.headers.value);
    const out = { label: f.label.value.trim() || f.name.value.trim(), color: f.color.value, mode: f.mode.value,
      weight: Number(f.weight.value) || 1, enabled: c.enabled !== false, search_page: f.search_page.value.trim() };
    if (f.mode.value !== "link") {
      Object.assign(out, { method: f.method.value, url: f.url.value.trim(), items_path: f.items_path.value.trim(),
        url_prefix: f.url_prefix.value.trim(), headers,
        fields: Object.fromEntries(Object.entries(fields).map(([k, i]) => [k, i.value.trim()]).filter(([, v]) => v)) });
      if (f.method.value === "POST") out.body = f.body.value.trim() || "{}";
    }
    return out;
  };

  const q = text("benchy", { "aria-label": "Тестовый запрос" });
  const result = el("div", { class: "test-result" });
  const testBtn = el("button", { type: "button", class: "btn" }, "Проверить");
  testBtn.addEventListener("click", async () => {
    let conf;
    try { conf = collect(); } catch (e) { toast("Заголовки: некорректный JSON — " + e.message, 6000); return; }
    testBtn.disabled = true;
    result.replaceChildren(el("p", { class: "muted" }, "Запрашиваю…"));
    try {
      const r = await api("/api/sources/test", { method: "POST", body: { config: conf, query: q.value, name: f.name.value || "test" } });
      result.replaceChildren(...renderTest(r, f, fields, () => testBtn.click()));
    } catch (e) { result.replaceChildren(el("p", { class: "err" }, e.message)); }
    testBtn.disabled = false;
  });

  apiBox.append(
    el("div", { class: "row-fields" }, field("Метод", f.method), field("Адрес API", f.url,
      "Подстановки: {q} — запрос, {limit}, {offset}, {page}, {page0} (страница с нуля).")),
    bodyWrap, field("Заголовки (JSON)", f.headers),
    el("div", { class: "row-fields" }, field("Где массив моделей (items_path)", f.items_path, "Путь через точку. «Проверить» подскажет."),
      field("Префикс для относительных ссылок", f.url_prefix)),
    el("h3", {}, "Поля модели"),
    el("p", { class: "muted small" }, "Путь к полю внутри элемента массива. Пустое — попробуем угадать. Для ссылки можно шаблон: https://site.com/m/{id}-{slug}."),
    el("div", { class: "fields-grid" }, ...FIELD_NAMES.map(([k, l]) => field(l, fields[k]))),
    el("div", { class: "inline test-bar" }, q, testBtn), result,
  );
  syncVis();

  const del = !isNew ? el("button", { type: "button", class: "btn ghost danger" }, "Удалить источник") : null;
  del?.addEventListener("click", async () => {
    if (!confirm(`Удалить источник «${c.label || name}»?`)) return;
    if (await patch({ custom_sources: { [name]: null } }, "Источник удалён")) onClose();
  });
  const cancel = el("button", { type: "button", class: "btn ghost" }, "Отмена");
  cancel.addEventListener("click", onClose);

  return panel(isNew ? "Новый источник" : `Источник «${c.label || name}»`,
    "Любой сайт с JSON API. Карточки с него ранжируются вместе с остальными.",
    el("div", { class: "row-fields" }, field("Имя (латиницей)", f.name), field("Подпись", f.label), field("Цвет", f.color)),
    el("div", { class: "row-fields" }, field("Режим", f.mode), field("Вес", f.weight)),
    field("Страница поиска на сайте (для ссылки)", f.search_page, "С {q}. Нужна для режима link и строки «Искать также на…»."),
    apiBox,
    saveRow("Сохранить источник", async () => {
      const id = f.name.value.trim();
      if (!/^[a-z0-9][a-z0-9_-]{0,39}$/.test(id)) { toast("Имя: латиница в нижнем регистре, цифры, - и _", 6000); return; }
      if (isNew && (S.config.sources?.[id] || S.config.custom_sources?.[id] || S.config.links?.[id])) { toast("Такое имя уже занято", 5000); return; }
      let conf;
      try { conf = collect(); } catch (e) { toast("Заголовки: некорректный JSON", 6000); return; }
      if (await patch({ custom_sources: { [id]: conf } }, "Источник сохранён")) onClose();
    }, [cancel, del].filter(Boolean)));
}

function renderTest(r, f, fields, retest) {
  const out = [];
  if (r.http_status) out.push(el("p", { class: "muted small" }, `HTTP ${r.http_status}`));
  if (r.error) out.push(el("p", { class: "err" }, r.error));
  if (r.arrays?.length && !r.ok) {
    out.push(el("p", {}, "Массивы в ответе — выберите, где лежат модели:"),
      el("div", { class: "chips-row" }, ...r.arrays.map((a) => el("button", { type: "button", class: "chip-btn",
        onclick: () => { f.items_path.value = a.path; retest(); } }, a.path || "(корень)", el("span", { class: "n" }, String(a.count))))));
  }
  if (r.ok) {
    const empty = Object.entries(r.fields_guess || {}).filter(([k]) => fields[k] && !fields[k].value);
    if (empty.length) {
      out.push(el("button", { type: "button", class: "btn small", onclick: () => {
        for (const [k, v] of empty) fields[k].value = v;
        retest();
      } }, `Подставить угаданные поля (${empty.map(([k]) => k).join(", ")})`));
    }
    out.push(el("p", { class: "ok" }, `Найдено ${r.count}. Так будут выглядеть карточки:`),
      el("div", { class: "sample-list" }, ...r.sample.map((h) => el("div", { class: "sample" },
        h.thumbnail ? el("img", { src: h.thumbnail, alt: "", loading: "lazy", referrerpolicy: "no-referrer" }) : el("span", { class: "noimg" }),
        el("div", {}, el("a", { href: h.url, target: "_blank", rel: "noopener noreferrer" }, h.title),
          el("small", { class: "muted" }, [h.author && "@" + h.author, h.likes != null && "♥ " + h.likes,
            h.downloads != null && "↓ " + h.downloads].filter(Boolean).join("  ")))))));
    const av = Object.entries(r.fields_available || {});
    if (av.length) {

      const assign = (path) => {
        const sel = el("select", { "aria-label": `Использовать ${path} как…`, class: "assign" },
          el("option", { value: "" }, "→ поле…"),
          ...FIELD_NAMES.map(([k, l]) => el("option", { value: k, selected: fields[k].value === path }, l.split(" (")[0])));
        sel.addEventListener("change", () => { if (sel.value) { fields[sel.value].value = path; retest(); } });
        return sel;
      };
      out.push(el("details", { class: "fields-avail", open: true }, el("summary", {}, `Поля первого элемента (${av.length}) — назначьте нужные`),
        el("table", {}, ...av.map(([k, v]) => el("tr", {}, el("td", {}, el("code", {}, k)),
          el("td", { class: "muted" }, String(v)), el("td", {}, assign(k)))))));
    }
  }
  return out;
}

let editing = null;
function sectionCustom() {
  if (editing) return [customEditor(editing.name, editing.conf, () => { editing = null; rerender(); })];
  const custom = S.config.custom_sources || {};
  const list = Object.entries(custom).map(([name, c]) => el("div", { class: "src-row" },
    el("span", { class: "dot", style: `background:${c.color || "#888"}` }),
    el("div", { class: "grow" }, el("b", {}, c.label || name), el("small", { class: "muted" }, ` ${c.mode || "http"} · ${c.url || c.search_page || ""}`)),
    el("button", { type: "button", class: "btn small", onclick: () => { editing = { name, conf: c }; rerender(); } }, "Изменить")));

  const links = Object.entries(S.config.links || {}).map(([name, l]) => ({ name, label: l.label || name, url: l.url || "", color: l.color || "#888888", orig: name }));
  const linkRows = el("div", { class: "links-edit" });
  const paintLinks = () => linkRows.replaceChildren(...links.map((l, i) => el("div", { class: "link-row" },
    el("input", { type: "color", value: l.color, "aria-label": "Цвет", oninput: (e) => { l.color = e.target.value; } }),
    el("input", { type: "text", value: l.label, placeholder: "Название", "aria-label": "Название", oninput: (e) => { l.label = e.target.value; } }),
    el("input", { type: "text", value: l.url, placeholder: "https://site.com/search?q={q}", "aria-label": "Адрес с {q}", oninput: (e) => { l.url = e.target.value; } }),
    el("button", { type: "button", class: "btn icon small", "aria-label": "Удалить " + l.label, onclick: () => { links.splice(i, 1); paintLinks(); } }, "✕"))));
  paintLinks();
  const addLink = el("button", { type: "button", class: "btn small ghost" }, "+ Добавить сайт");
  addLink.addEventListener("click", () => { links.push({ name: "", label: "", url: "", color: "#888888" }); paintLinks(); });

  return [
    panel("Свои источники с API", "Сайты с JSON-ответом: настраиваются без кода, с проверкой прямо здесь.",
      list.length ? el("div", { class: "src-list" }, ...list) : el("p", { class: "muted" }, "Пока нет."),
      saveRow("Добавить источник", async () => { editing = { name: "", conf: {} }; rerender(); })),
    panel("Сайты-ссылки", "Не опрашиваются: появляются в строке «Искать также на…» под поиском. {q} заменится на запрос.",
      linkRows, addLink,
      saveRow("Сохранить ссылки", async () => {
        const p = {};
        const taken = new Set(Object.keys(S.config.sources || {}).concat(Object.keys(S.config.custom_sources || {})));
        for (const l of links) {
          const id = l.orig || (l.label || "link").toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "") || "link";
          let uniq = id, n = 2;
          while (!l.orig && (taken.has(uniq) || p[uniq])) uniq = `${id}-${n++}`;
          p[uniq] = { label: l.label, url: l.url.trim(), color: l.color };
        }
        for (const old of Object.keys(S.config.links || {})) if (!links.some((l) => l.orig === old)) p[old] = null;
        await patch({ links: p }, "Ссылки сохранены");
        rerender();
      })),
  ];
}

const SIGNAL_LABELS = { relevance: "Совпадение слов", semantic: "Смысловая близость", source_rank: "Позиция на сайте",
  popularity: "Популярность", freshness: "Свежесть", personal: "Похоже на избранное" };

function sectionRanking() {
  const r = S.config.ranking, fl = S.config.filters;
  const w = Object.fromEntries(Object.keys(SIGNAL_LABELS).map((k) => [k, num(r.weights[k] ?? 0, { min: 0, max: 5, step: 0.05 })]));
  const hl = num(r.freshness_half_life_days, { min: 1, max: 10000 });
  const dlf = num(r.downloads_factor, { min: 0, max: 10, step: 0.05 });
  const dd = check(r.dedupe, "Склеивать одну модель с разных сайтов (по названию и автору)");
  const pillow = extras?.items?.find((i) => i.key === "pillow")?.installed;
  const ddi = check(r.dedupe_images, "…и по похожести превью" + (pillow ? "" : " (нужен Pillow — «Компоненты»)"), { disabled: !pillow && !r.dedupe_images });
  const ex = text((fl.exclude_words || []).join(", "), { placeholder: "nsfw" });
  const ml = num(fl.min_likes || 0, { min: 0 });
  const fo = check(fl.free_only, "Только бесплатные");
  return [
    panel("Веса по умолчанию", "Стартовые значения для ползунков в «Фильтры и веса». Если вы уже двигали ползунки в этом браузере, нажмите там «Сбросить всё к config.yaml», чтобы подхватить новые.",
      el("div", { class: "fields-grid" }, ...Object.entries(w).map(([k, i]) => field(SIGNAL_LABELS[k], i))),
      el("div", { class: "row-fields" }, field("Период полураспада свежести, дней", hl), field("Скачивание = N лайков", dlf)),
      dd.node, ddi.node,
      saveRow("Сохранить", () => patch({ ranking: { weights: Object.fromEntries(Object.entries(w).map(([k, i]) => [k, Number(i.value) || 0])),
        freshness_half_life_days: Number(hl.value), downloads_factor: Number(dlf.value), dedupe: dd.input.checked, dedupe_images: ddi.input.checked } }))),
    panel("Фильтры по умолчанию", null,
      field("Исключать слова", ex), field("Минимум лайков", ml), fo.node,
      saveRow("Сохранить", () => patch({ filters: { exclude_words: ex.value.split(",").map((x) => x.trim()).filter(Boolean),
        min_likes: Number(ml.value) || 0, free_only: fo.input.checked } }))),
  ];
}

function sectionSmart() {
  const sem = S.config.semantic, tr = S.config.translate, fs = S.features.semantic;
  const fastembed = extras?.items?.find((i) => i.key === "fastembed")?.installed;
  const semOn = check(sem.enabled, "Включить смысловой поиск");
  const semModel = text(sem.model);
  const semJob = jobConsole();
  const semActions = el("div", { class: "inline" });
  if (!fastembed) {
    semActions.append(el("button", { type: "button", class: "btn", onclick: async () => {
      const st = await semJob.run(api("/api/extras/install", { method: "POST", body: { keys: ["fastembed"] } }));
      if (st === "ok") { await refreshExtras(); rerender(); }
    } }, "Установить fastembed"));
  } else if (fs.available) {
    semActions.append(el("button", { type: "button", class: "btn", onclick: () => semJob.run(api("/api/semantic/warm", { method: "POST" })) },
      fs.loaded ? "Модель загружена ✓" : "Скачать модель сейчас"));
  }
  const semPanel = panel("Смысловой поиск — дополнительный модуль",
    "Не входит в базовую установку. Локальная мультиязычная модель (~220 МБ, работает на процессоре): понимает, что «держатель наушников» и «headphone holder» — одно и то же. Поиск по картинке не требует этого модуля: он идёт через сервисы, которые это умеют (кнопка с камерой у строки поиска).",
    el("p", {}, "Статус: ", fastembed ? pill(true, "fastembed установлен", "") : pill(false, "", "fastembed не установлен"), " ",
      sem.enabled ? (fs.available ? pill(true, "включён", "") : pill(false, "", fs.error || "ошибка")) : pill(false, "", "выключен")),
    semOn.node, field("Модель", semModel, "Любая модель из списка fastembed."), semActions, semJob,
    saveRow("Сохранить", async () => { await patch({ semantic: { enabled: semOn.input.checked, model: semModel.value.trim() } }); rerender(); }));

  const trOn = check(tr.enabled, "Переводить русские запросы на английский");
  const trUrl = text(tr.url);
  const trModel = text(tr.model);
  const trKey = text(tr.api_key_env || "", { placeholder: "не нужен для Ollama" });
  const ollamaBox = el("div", { class: "ollama" }, el("p", { class: "muted" }, "Проверяю Ollama…"));
  const job = jobConsole();
  const trPanel = panel("Перевод запросов",
    "Сайты ищут по-английски намного лучше. Переводит локальная модель через Ollama или любой OpenAI-совместимый API (LM Studio, облако).",
    trOn.node,
    el("div", { class: "row-fields" }, field("Адрес API", trUrl, "Ollama: http://127.0.0.1:11434/v1"), field("Модель", trModel, "Для Ollama: qwen2.5:3b (≈2 ГБ) или qwen2.5:1.5b")),
    field("Переменная с ключом API (если нужен)", trKey),
    tr.api_key_env ? secretField(tr.api_key_env, "Ключ API") : null,
    el("h3", {}, "Ollama на этом компьютере"), ollamaBox, job,
    saveRow("Сохранить", async () => { await patch({ translate: { enabled: trOn.input.checked, url: trUrl.value.trim(), model: trModel.value.trim(), api_key_env: trKey.value.trim() } }); rerender(); }));

  paintOllama(ollamaBox, job);
  return [trPanel, semPanel];
}

async function paintOllama(box, job) {
  let o;
  try { o = await api("/api/ollama"); } catch (e) { box.replaceChildren(el("p", { class: "err" }, e.message)); return; }
  const hasModel = o.models.some((m) => m.name === o.model || m.name === o.model + ":latest");
  const rows = [
    el("li", {}, "Установлена: ", pill(!!o.binary, "да", "нет")),
    el("li", {}, "Запущена: ", pill(o.running, `да, версия ${o.version}`, "нет"), el("span", { class: "muted" }, ` (${o.base})`)),
    el("li", {}, `Модель ${o.model}: `, o.running ? pill(hasModel, "скачана", "не скачана") : el("span", { class: "muted" }, "—")),
  ];
  const actions = el("div", { class: "inline" });
  const again = () => paintOllama(box, job);
  if (!o.binary && !o.running) {
    if (o.install.auto) {
      actions.append(el("button", { type: "button", class: "btn", onclick: async () => {
        if (!confirm(`Установить Ollama через ${o.install.how}? Это примет соглашения источника пакетов.`)) return;
        await job.run(api("/api/ollama/install", { method: "POST" })); again();
      } }, `Установить Ollama (${o.install.how})`));
    } else {
      actions.append(el("p", { class: "muted" },
        o.install.how === "docker" ? "modelseek работает в Docker. Запустите Ollama рядом: " : o.install.how === "terminal" ? "Установите в терминале: " : "Скачайте установщик: ",
        o.install.command.startsWith("http") ? el("a", { href: o.install.command, target: "_blank", rel: "noopener" }, o.install.command)
          : el("code", {}, o.install.command)));
    }
  }
  if (o.binary && !o.running) {
    actions.append(el("button", { type: "button", class: "btn", onclick: async () => { await job.run(api("/api/ollama/start", { method: "POST" })); again(); } }, "Запустить Ollama"));
  }
  if (o.running && !hasModel) {
    actions.append(el("button", { type: "button", class: "btn", onclick: async () => {
      await job.run(api("/api/ollama/pull", { method: "POST", body: { model: o.model } })); again();
    } }, `Скачать ${o.model}`));
  }
  if (o.running && hasModel && !o.enabled) {
    actions.append(el("button", { type: "button", class: "btn primary", onclick: async () => { await patch({ translate: { enabled: true } }, "Перевод включён"); rerender(); } }, "Включить перевод"));
  }
  actions.append(el("button", { type: "button", class: "btn ghost small", onclick: again }, "Обновить"));
  box.replaceChildren(el("ul", { class: "status-list" }, ...rows), actions);
}

async function refreshExtras() { extras = await api("/api/extras").catch(() => extras); }

function sectionComponents() {
  const job = jobConsole();
  const boxes = {};
  const rows = (extras?.items || []).map((i) => {
    const cb = el("input", { type: "checkbox", disabled: i.installed, checked: false });
    boxes[i.key] = cb;
    return el("label", { class: "comp-row" + (i.installed ? " done" : "") }, cb,
      el("div", { class: "grow" }, el("b", {}, i.package), el("small", { class: "muted" }, " — " + i.why),
        i.key === "playwright" && i.installed ? el("div", {}, "Chromium: ", pill(i.chromium, "скачан", "не скачан")) : null),
      pill(i.installed, "установлен", "нет"));
  });
  const pw = extras?.items?.find((i) => i.key === "playwright");
  const chromium = pw?.installed && !pw.chromium ? el("button", { type: "button", class: "btn", onclick: async () => {
    if (await job.run(api("/api/extras/chromium", { method: "POST" })) === "ok") { await refreshExtras(); rerender(); }
  } }, "Скачать Chromium (~150 МБ)") : null;
  return [panel("Необязательные компоненты",
    `Ставятся в окружение, из которого запущен modelseek (Python ${extras?.python || "?"}${extras?.venv ? ", .venv" : ""}${extras?.docker ? ", Docker: в том data" : ""}). После установки включаются без перезапуска.`,
    el("div", { class: "comp-list" }, ...rows), job,
    saveRow("Установить выбранное", async () => {
      const keys = Object.entries(boxes).filter(([, cb]) => cb.checked).map(([k]) => k);
      if (!keys.length) { toast("Отметьте, что установить"); return; }
      const st = await job.run(api("/api/extras/install", { method: "POST", body: { keys } }));
      await refreshExtras();
      if (st === "ok") { await onSaved(); rerender(); }
    }, chromium ? [chromium] : []),
    el("p", { class: "muted small" }, "Если установка из интерфейса не проходит (нет прав, Docker), запустите install-extras.bat или соберите образ с EXTRAS=1."))];
}

function sectionNotify() {
  const n = S.config.notify, sub = S.config.subscriptions;
  const every = num(sub.check_every_seconds, { min: 0, max: 86400 });
  const kind = select(n.kind || "", [["", "Не отправлять"], ["ntfy", "ntfy (приложение на телефон)"], ["telegram", "Telegram-бот"], ["webhook", "Webhook (POST JSON)"]]);
  const url = text(n.url, { placeholder: "https://ntfy.sh/мой-секретный-топик" });
  const chat = text(n.chat_id, { placeholder: "123456789" });
  const tokenEnv = text(n.token_env || "", { placeholder: "TELEGRAM_BOT_TOKEN" });
  const openUrl = text(n.open_url);
  const urlF = field("Адрес", url), chatF = field("chat_id", chat, "Напишите боту, затем откройте https://api.telegram.org/bot<токен>/getUpdates — там будет chat.id.");
  const tokF = field("Переменная с токеном", tokenEnv, "Для Telegram — токен бота; для ntfy — токен доступа, если топик закрыт.");
  const sync = () => { urlF.hidden = !["ntfy", "webhook"].includes(kind.value); chatF.hidden = kind.value !== "telegram"; tokF.hidden = !kind.value || kind.value === "webhook"; };
  kind.addEventListener("change", sync);
  sync();
  const test = el("button", { type: "button", class: "btn ghost", onclick: async () => {
    try { await api("/api/notify/test", { method: "POST" }); toast("Тест отправлен"); } catch (e) { toast("Не отправилось: " + e.message, 6000); }
  } }, "Отправить тест");
  return [
    panel("Подписки", null, field("Проверять расписание каждые, секунд", every, "0 — не проверять подписки автоматически."),
      saveRow("Сохранить", () => patch({ subscriptions: { check_every_seconds: Number(every.value) || 0 } }))),
    panel("Уведомления о новых моделях", null, field("Куда", kind), urlF, chatF, tokF,
      n.token_env ? secretField(n.token_env, "Токен") : null,
      field("Ссылка в уведомлении ведёт на", openUrl, "Адрес modelseek, как его видно с телефона (например, по Tailscale)."),
      saveRow("Сохранить", async () => { await patch({ notify: { kind: kind.value, url: url.value.trim(), chat_id: chat.value.trim(), token_env: tokenEnv.value.trim(), open_url: openUrl.value.trim() } }); rerender(); }, [test])),
  ];
}

function sectionServer() {
  const sv = S.config.server, br = S.config.breaker, bw = S.config.browser, im = S.config.impersonate;
  const i = {
    timeout: num(sv.request_timeout_seconds, { min: 1, max: 120 }), limit: num(sv.per_source_limit, { min: 1, max: 100 }),
    ttl: num(sv.cache_ttl_seconds, { min: 0, max: 86400 }), ua: text(sv.user_agent),
    fails: num(br.max_failures, { min: 0, max: 100 }), cool: num(br.cooldown_minutes, { min: 0, max: 1440 }),
    exe: text(bw.executable_path, { placeholder: "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe" }),
    nav: num(bw.nav_timeout_seconds, { min: 5, max: 120 }), target: text(im.target),
  };
  const headless = check(bw.headless, "Браузер без окна (окно открывается только для проверки)");
  const reset = el("button", { type: "button", class: "btn ghost danger", onclick: async () => {
    if (!confirm("Сбросить все настройки, изменённые в интерфейсе? config.yaml и ключи не трогаются.")) return;
    S = await api("/api/settings/reset", { method: "POST", body: {} });
    await onSaved(); toast("Сброшено к config.yaml"); rerender();
  } }, "Сбросить всё к config.yaml");
  return [
    panel("Запросы к сайтам", null,
      el("div", { class: "fields-grid" }, field("Таймаут, с", i.timeout), field("Результатов с сайта", i.limit), field("Кэш, с", i.ttl)),
      field("User-Agent", i.ua),
      el("div", { class: "fields-grid" }, field("Ошибок подряд до паузы", i.fails, "0 — не отключать"), field("Пауза, мин", i.cool)),
      saveRow("Сохранить", () => patch({ server: { request_timeout_seconds: Number(i.timeout.value), per_source_limit: Number(i.limit.value),
        cache_ttl_seconds: Number(i.ttl.value), user_agent: i.ua.value.trim() }, breaker: { max_failures: Number(i.fails.value), cooldown_minutes: Number(i.cool.value) } }))),
    panel("Режимы browser и impersonate", null, headless.node,
      field("Свой Chrome/Edge вместо встроенного Chromium", i.exe, "Пусто — встроенный Chromium Playwright."),
      el("div", { class: "fields-grid" }, field("Загрузка страницы, с", i.nav), field("Отпечаток curl_cffi", i.target, "chrome, edge, safari…")),
      saveRow("Сохранить", () => patch({ browser: { headless: headless.input.checked, executable_path: i.exe.value.trim(), nav_timeout_seconds: Number(i.nav.value) },
        impersonate: { target: i.target.value.trim() || "chrome" } }))),
    panel("Файлы", "Интерфейс не переписывает config.yaml — изменения лежат отдельно и перекрывают его.",
      el("ul", { class: "status-list" },
        el("li", {}, "Ваш конфиг: ", el("code", {}, S.paths.config)),
        el("li", {}, "Изменения из интерфейса: ", el("code", {}, S.paths.overrides)),
        el("li", {}, "Ключи: ", el("code", {}, S.paths.secrets))),
      el("div", { class: "save-row" }, reset)),
  ];
}

const SECTIONS = { look: sectionLook, sources: sectionSources, custom: sectionCustom, ranking: sectionRanking,
  smart: sectionSmart, components: sectionComponents, notify: sectionNotify, server: sectionServer };
let current = "look";

function rerender() {
  const body = $("#settings-body");
  const y = window.scrollY;
  body.replaceChildren(...SECTIONS[current]().filter(Boolean));
  window.scrollTo(0, y);
}

export async function showSettings(section) {
  current = SECTIONS[section] ? section : "look";
  if (current !== "custom") editing = null;
  $$(".settings-nav a").forEach((a) => a.classList.toggle("active", a.getAttribute("href") === "#settings/" + current));
  const body = $("#settings-body");
  if (!S) body.replaceChildren(el("p", { class: "muted" }, "Загружаю…"));
  try {
    [S, extras] = await Promise.all([api("/api/settings"), api("/api/extras").catch(() => null)]);
  } catch (e) { body.replaceChildren(el("p", { class: "err" }, "Не удалось загрузить настройки: " + e.message)); return; }
  rerender();
}
