import { $, GUEST, SIGNALS, api, debounce, el, state, toast } from "./util.js";
import { addHistory, personalize } from "./guest.js";
import { reconcile, renderCard } from "./cards.js";
import { save, toggleWord } from "./settings.js";
import { refreshSubsBadge } from "./subs.js";

const cur = { query: "", page: 1, es: null, reqId: 0, pages: [], statuses: {}, lastSnapshot: null };

export function searchParams(page = 1, query = cur.query) {
  const s = state.settings, f = s.filters;
  const p = new URLSearchParams({ q: query, page, sort: s.sort, half_life: s.half_life, dedupe: s.dedupe ? "1" : "0" });
  p.set("sources", s.enabled.join(","));
  for (const [k] of SIGNALS) p.set("w_" + k, s.weights[k] ?? 0);
  for (const [k, v] of Object.entries(s.sourceWeights)) p.set("sw_" + k, v);
  p.set("price", f.price);
  if (f.max_price != null) p.set("max_price", f.max_price);
  for (const k of ["min_likes", "min_downloads", "min_makes", "days"]) if (f[k]) p.set(k, f[k]);
  if (f.license !== "any") p.set("license", f.license);
  for (const k of ["include", "exclude", "exclude_authors"]) if (f[k].length) p.set(k, f[k].join(","));
  if (f.has_image) p.set("has_image", "1");
  if (f.hide_seen) p.set("hide_seen", "1");
  return p;
}

function subscriptionParams() {
  const s = state.settings;
  return { sources: s.enabled, sort: s.sort, weights: s.weights, source_weights: s.sourceWeights,
    half_life: s.half_life, filters: s.filters, dedupe: s.dedupe };
}

export function renderChips() {
  const s = state.settings;
  const polled = state.sources.filter((x) => !x.link_only);
  $("#sources").replaceChildren(...polled.map((src) => {
    const on = s.enabled.includes(src.name) && src.configured;
    const st = cur.statuses[src.name];
    const b = el("button", { type: "button", class: "chip", style: `--c:${src.color}`, "aria-pressed": String(on) },
      el("span", { class: "dot" }), el("span", {}, src.label));
    let tail = null;
    if (!src.configured) {
      b.classList.add("disabled");
      b.title = GUEST ? "Нужен ваш ключ этого сайта: Настройки → Ключи сайтов" : "Нужен ключ в .env: " + src.missing.join(", ");
      tail = el("span", { class: "st" }, "нет ключа");
    } else if (st && on) {
      const map = {
        pending: () => el("span", { class: "st spin", "aria-label": "загрузка" }),
        ok: () => el("span", { class: "st ok" }, `${st.count}${st.cached ? " · кэш" : ` · ${st.ms}мс`}`),
        error: () => el("span", { class: "st err" }, "ошибка"),
        paused: () => el("span", { class: "st warn" }, "пауза"),
        challenge: () => el("span", { class: "st warn" }, "проверка"),
        skipped: () => el("span", { class: "st" }, "нет ключа"),
      };
      tail = (map[st.status] || (() => null))();
      if (st.error) b.title = st.error;
      else if (st.via === "browser") b.title = "Ваш браузер спросил сайт сам";
    }
    if (src.mode === "browser" || src.mode === "impersonate") b.append(el("span", { class: "mode", title: "режим " + src.mode }, src.mode === "browser" ? "🌐" : "🎭"));
    if (tail) b.append(tail);
    b.addEventListener("click", () => {
      if (!src.configured) { if (GUEST) location.hash = "#settings/look"; return; }
      const set = new Set(s.enabled);
      set.has(src.name) ? set.delete(src.name) : set.add(src.name);
      s.enabled = [...set];
      save();
      renderChips();
    });
    const wrap = [b];
    if (GUEST) {
      // the browser check and the breaker reset belong to the owner
    } else if (st && on && st.status === "challenge" && st.can_open) {
      wrap.push(el("button", { type: "button", class: "btn small warn", onclick: () => openBrowser(src) }, "Пройти проверку"));
    } else if (st && on && st.status === "paused") {
      wrap.push(el("button", { type: "button", class: "btn small ghost", title: "Попробовать снова сейчас",
        onclick: async () => { await api(`/api/sources/${src.name}/reset`, { method: "POST" }); rerun(); } }, "↻"));
    }
    return wrap.length > 1 ? el("span", { class: "chip-wrap" }, ...wrap) : b;
  }));
  renderLinks();
}

function renderLinks() {
  const row = $("#links-row");
  if (!cur.query) { row.hidden = true; return; }
  const q = encodeURIComponent(cur.query).replace(/%20/g, "+");
  const items = state.sources.filter((x) => x.mode === "link" && x.enabled !== false).map((src) => {
    const st = cur.statuses[src.name];
    const href = st?.link || (src.search_page ? src.search_page.replace("{q}", q) : null);
    if (!href) return null;
    return el("a", { href, target: "_blank", rel: "noopener noreferrer", class: "ext", style: `--c:${src.color}` }, src.label, " ↗");
  }).filter(Boolean);
  row.replaceChildren(el("span", { class: "muted" }, "Искать также на:"), ...items);
  row.hidden = items.length === 0;
}

async function openBrowser(src) {
  try {
    await api("/api/browser/open", { method: "POST", body: { source: src.name } });
  } catch (e) { toast("Не удалось открыть браузер: " + e.message, 6000); return; }
  toast(`Открылось окно браузера с ${src.label}. Пройдите проверку и закройте окно — поиск повторится.`, 9000);
  const t = setInterval(async () => {
    const st = await api("/api/browser/status").catch(() => null);
    if (st && !st.waiting_user) { clearInterval(t); await api("/api/cache/clear", { method: "POST" }); rerun(); }
  }, 2000);
}

export function rerun() { if (cur.query) search(cur.query, 1); }
export const rerunSoon = debounce(rerun, 250);

// A site that allows it (CORS) is asked by the guest's own browser, with their key and from their address;
// the server only parses and ranks the answer. If the site refuses or is silent, the server asks it instead.
async function fetchInBrowser(params) {
  if (!GUEST || !state.sources.some((x) => x.browser_direct && x.own_keys?.length)) return null;
  let plan;
  try { plan = await api("/api/search/plan", { query: params.toString() }); } catch { return null; }
  if (!plan?.token) return null;
  await Promise.all(plan.requests.map(async (r) => {
    const ctl = new AbortController();
    const timer = setTimeout(() => ctl.abort(), 8000);
    try {
      const resp = await fetch(r.url, { method: r.method, headers: r.headers, body: r.body ?? undefined, mode: "cors",
        credentials: "omit", referrerPolicy: "no-referrer", cache: "no-store", signal: ctl.signal });
      const text = await resp.text();
      if (text.length <= 2_000_000) {
        await api("/api/search/relay", { method: "POST", body: { token: plan.token, source: r.source, status: resp.status, text } });
      }
    } catch { /* refused by the site (CORS) or no answer: the server will ask */ }
    finally { clearTimeout(timer); }
  }));
  return plan.token;
}

export async function search(query, page = 1) {
  query = (query || "").trim();
  if (!query) return;
  if (cur.es) cur.es.close();
  const id = ++cur.reqId;
  const newQuery = query !== cur.query;
  cur.query = query;
  cur.page = page;
  $("#empty").hidden = true;
  $("#subscribe").disabled = false;
  $("#subscribe").title = "Получать новые модели по этому запросу";
  if (page === 1) {
    cur.pages = [];
    if (newQuery) { cur.statuses = {}; showSkeletons(); $("#facets").hidden = true; }
    $("#summary").textContent = "Ищу…";
  }
  $("#more").disabled = true;
  if (location.hash !== "#search" || new URLSearchParams(location.search).get("q") !== query) {
    history.replaceState(null, "", "?q=" + encodeURIComponent(query) + "#search");
  }

  const params = searchParams(page, query);
  const relayed = await fetchInBrowser(params);
  if (id !== cur.reqId) return;
  if (relayed) params.set("pf", relayed);
  const es = new EventSource("/api/search/stream?" + params);
  cur.es = es;
  es.addEventListener("update", (ev) => {
    if (id !== cur.reqId) return;
    const snap = JSON.parse(ev.data);
    if (GUEST) {
      snap.results = personalize(snap.results, state.settings.filters);
      if (snap.done && page === 1) addHistory(query, snap.results.length);
    }
    cur.statuses = Object.fromEntries(snap.sources.map((s) => [s.name, s]));
    cur.pages[page - 1] = snap.results;
    cur.lastSnapshot = snap;
    renderChips();
    render(snap);
  });
  es.addEventListener("end", () => { es.close(); if (id === cur.reqId) finish(); });
  es.addEventListener("fail", (ev) => {
    es.close();
    if (id !== cur.reqId) return;
    let msg = "поиск не выполнен";
    try { msg = JSON.parse(ev.data).error || msg; } catch {}
    $("#summary").textContent = msg[0].toUpperCase() + msg.slice(1);
    if (page === 1 && !cur.lastSnapshot) $("#results").replaceChildren();
    finish();
  });
  es.onerror = async () => {
    es.close();
    if (id !== cur.reqId) return;
    if (GUEST) {
      const s = await api("/api/session").catch(() => null);
      if (s && !s.logged_in) { document.dispatchEvent(new CustomEvent("need-login")); $("#summary").textContent = "Нужен пароль"; finish(); return; }
    }
    if (!cur.lastSnapshot) $("#summary").textContent = "Сервер не отвечает — запущен ли modelseek?";
    finish();
  };
}

function finish() {
  $("#more").disabled = false;
  const hasMore = (cur.pages[cur.page - 1] || []).length > 0;
  $("#more").hidden = !hasMore;
}

function showSkeletons() {
  $("#results").replaceChildren(...Array.from({ length: 10 }, () => el("div", { class: "skeleton" })));
}

function allResults() {
  const seen = new Set();
  const out = [];
  for (const page of cur.pages) for (const h of page || []) if (!seen.has(h.key)) { seen.add(h.key); out.push(h); }
  return out;
}

function render(snap) {
  const hits = allResults();
  const grid = $("#results");
  if (grid.querySelector(".skeleton") && (hits.length || snap.done)) grid.replaceChildren();
  reconcile(grid, hits, (h) => renderCard(h, { onHide: hideHit, onHideAuthor: hideAuthor }));

  if (snap.page === 1) renderFacets(snap.facets || []);
  const errs = snap.sources.filter((s) => ["error", "challenge"].includes(s.status)).map((s) => s.label);
  const pending = snap.sources.filter((s) => s.status === "pending").length;
  let text = `${hits.length} моделей · ${snap.took_ms} мс`;
  if (snap.translated) text += ` · искали по-английски: «${snap.translated}»`;
  if (pending) text += ` · ждём ещё ${pending}`;
  if (errs.length) text += ` · не ответили: ${errs.join(", ")}`;
  if (snap.done && !hits.length) text += " · ничего не найдено. Попробуйте запрос на английском или ослабьте фильтры";
  $("#summary").textContent = text;
}

function renderFacets(facets) {
  const box = $("#facets");
  const f = state.settings.filters;
  const active = [...f.include.map((w) => ["include", w]), ...f.exclude.map((w) => ["exclude", w])];
  const shown = new Set(active.map(([, w]) => w));
  const chips = facets.filter((x) => !shown.has(x.term)).map((x) => el("span", { class: "facet" },
    el("button", { type: "button", class: "facet-in", title: `Только «${x.term}»`, onclick: () => toggleWord("include", x.term) },
      x.term, el("span", { class: "n" }, String(x.count))),
    el("button", { type: "button", class: "facet-out", title: `Без «${x.term}»`, "aria-label": `Исключить ${x.term}`,
      onclick: () => toggleWord("exclude", x.term) }, "−")));
  const act = active.map(([k, w]) => el("button", { type: "button", class: "facet active " + k,
    onclick: () => toggleWord(k, w) }, (k === "include" ? "✓ " : "− ") + w, " ×"));
  box.replaceChildren(el("span", { class: "muted" }, "Темы:"), ...act, ...chips);
  box.hidden = chips.length + act.length === 0;
}

async function hideHit(h, node) {
  await api("/api/hidden", { method: "POST", body: { key: h.key, title: h.title } });
  node.classList.add("leaving");
  setTimeout(() => {
    for (const p of cur.pages) { const i = (p || []).findIndex((x) => x.key === h.key); if (i >= 0) p.splice(i, 1); }
    node.remove();
  }, 180);
  toast("Скрыто. Вернуть можно в «Фильтры и веса → Сервис»");
}

function hideAuthor(author) {
  if (!confirm(`Скрыть все модели автора @${author}?`)) return;
  const f = state.settings.filters;
  if (!f.exclude_authors.includes(author.toLowerCase())) f.exclude_authors.push(author.toLowerCase());
  state.settings.preset = null;
  save();
  toast(`Автор @${author} скрыт`);
}

async function showHistory() {
  const pop = $("#history-pop");
  let items = [];
  try { items = (await api("/api/history")).items; } catch { return; }
  if (document.activeElement !== $("#q")) { pop.hidden = true; return; }
  const typed = $("#q").value.trim().toLowerCase();
  items = items.filter((h) => !typed || h.query.toLowerCase().includes(typed)).slice(0, 10);
  if (!items.length) { pop.hidden = true; return; }
  pop.replaceChildren(...items.map((h) => el("div", { class: "hist-row", role: "option" },
    el("button", { type: "button", class: "hist-q", onmousedown: (e) => { e.preventDefault(); pick(h.query); } },
      h.query, el("span", { class: "muted" }, ` · ${h.results ?? "?"}`)),
    el("button", { type: "button", class: "hist-del", "aria-label": "Удалить из истории", onmousedown: async (e) => {
      e.preventDefault();
      await api("/api/history", { method: "DELETE", query: { query: h.query } });
      showHistory();
    } }, "✕"))));
  pop.hidden = false;
}
function pick(q) { $("#q").value = q; $("#history-pop").hidden = true; search(q); }

function openSubscribeDialog() {
  const dlg = $("#sub-dialog");
  $("#sub-query").value = cur.query;
  const sel = $("#sub-interval");
  sel.replaceChildren(...state.config.intervals.map((i) => el("option", { value: i.minutes, selected: i.minutes === 1440 }, i.label)),
    el("option", { value: "custom" }, "своё…"));
  $("#sub-custom-wrap").hidden = true;
  sel.onchange = () => { $("#sub-custom-wrap").hidden = sel.value !== "custom"; };
  dlg.showModal();
}

async function createSub(e) {
  if (e.submitter?.value !== "ok") return;
  e.preventDefault();
  const sel = $("#sub-interval");
  const interval = sel.value === "custom" ? Math.max(15, Number($("#sub-custom").value) || 180) : Number(sel.value);
  const btn = $("#sub-create");
  btn.disabled = true;
  btn.textContent = "Запоминаю текущую выдачу…";
  try {
    const r = await api("/api/subs", { method: "POST",
      body: { query: $("#sub-query").value.trim(), interval_min: interval, params: subscriptionParams() } });
    $("#sub-dialog").close();
    toast(`Подписка создана: сейчас ${r.sub.total} моделей, дальше — только новые`, 5000);
    refreshSubsBadge();
  } catch (err) { toast("Ошибка: " + err.message, 5000); }
  btn.disabled = false;
  btn.textContent = "Подписаться";
}

export function loadIntoSearch(query, params) {
  const s = state.settings;
  if (params) {
    if (params.sources) s.enabled = params.sources.filter((n) => state.sources.some((x) => x.name === n && x.configured));
    if (params.weights) s.weights = { ...s.weights, ...params.weights };
    if (params.source_weights) s.sourceWeights = { ...s.sourceWeights, ...params.source_weights };
    if (params.filters) s.filters = { ...s.filters, ...params.filters };
    if (params.sort) s.sort = params.sort;
    s.preset = null;
  }
  $("#q").value = query;
  location.hash = "#search";
  search(query);
}

export function bindSearch() {
  $("#search-form").addEventListener("submit", (e) => {
    e.preventDefault();
    $("#history-pop").hidden = true;
    $("#q").blur();
    if (location.hash !== "#search") location.hash = "#search";
    search($("#q").value);
  });
  const q = $("#q");
  q.addEventListener("focus", showHistory);
  q.addEventListener("input", debounce(showHistory, 150));
  q.addEventListener("blur", () => setTimeout(() => { $("#history-pop").hidden = true; }, 120));
  q.addEventListener("keydown", (e) => { if (e.key === "Escape") $("#history-pop").hidden = true; });
  $("#more").addEventListener("click", () => search(cur.query, cur.page + 1));
  document.querySelectorAll(".examples button").forEach((b) => b.addEventListener("click", () => { q.value = b.dataset.q; search(b.dataset.q); }));
  $("#subscribe").addEventListener("click", openSubscribeDialog);
  $("#sub-form").addEventListener("submit", createSub);
  document.addEventListener("keydown", (e) => {
    const tag = document.activeElement?.tagName;
    if (e.key === "/" && !["INPUT", "TEXTAREA", "SELECT"].includes(tag)) { e.preventDefault(); q.focus(); }
  });
}

export const currentQuery = () => cur.query;
