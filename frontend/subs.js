import { $, ago, api, el, state, toast, when } from "./util.js";
import { renderCard } from "./cards.js";
import { loadIntoSearch } from "./search.js";

export async function refreshSubsBadge() {
  try {
    const d = await api("/api/subs");
    const b = $("#subs-new");
    b.hidden = !d.new_total;
    b.textContent = d.new_total ? String(d.new_total) : "";
    document.title = d.new_total ? `(${d.new_total}) modelseek` : "modelseek";
    return d;
  } catch { return null; }
}

function intervalLabel(min) {
  const known = state.config.intervals.find((i) => i.minutes === min);
  if (known) return known.label;
  return min % 60 === 0 ? `каждые ${min / 60} ч` : `каждые ${min} мин`;
}

function filterSummary(p) {
  const f = p.filters || {};
  const parts = [];
  if (f.price === "free") parts.push("бесплатно");
  if (f.price === "paid") parts.push("платно");
  if (f.min_likes) parts.push(`♥≥${f.min_likes}`);
  if (f.min_makes) parts.push(`🖨≥${f.min_makes}`);
  if (f.days) parts.push(`${f.days} дн.`);
  if (f.license && f.license !== "any") parts.push("лицензия");
  if (f.include?.length) parts.push("+" + f.include.join(", +"));
  if (f.exclude?.length) parts.push("−" + f.exclude.join(", −"));
  return parts.join(" · ") || "без фильтров";
}

export async function showSubs() {
  const d = await refreshSubsBadge();
  if (!d) return;
  $("#sub-detail").hidden = true;
  $("#subs-list").hidden = false;
  const n = state.config.features.notify;
  $("#notify-state").replaceChildren(n.enabled
    ? el("span", {}, `Уведомления: ${n.kind}. `, el("button", { type: "button", class: "linkish", onclick: testNotify }, "отправить тест"))
    : el("span", {}, "Уведомления не настроены (Настройки → Подписки и уведомления)."));
  $("#subs-empty").hidden = d.items.length > 0;
  $("#subs-list").replaceChildren(...d.items.map(subCard));
}

async function testNotify() {
  try { await api("/api/notify/test", { method: "POST" }); toast("Тест отправлен"); }
  catch (e) { toast("Не отправилось: " + e.message, 5000); }
}

function subCard(s) {
  const sel = el("select", { "aria-label": "Периодичность", onchange: async () => {
    let v = sel.value;
    if (v === "custom") {
      const m = Number(prompt("Каждые сколько минут проверять? (от 15)", String(s.interval_min)));
      if (!m) { sel.value = String(s.interval_min); return; }
      v = Math.max(15, m);
    }
    await api(`/api/subs/${s.id}`, { method: "PATCH", body: { interval_min: Number(v) } });
    toast("Периодичность: " + intervalLabel(Number(v)));
    showSubs();
  } },
  ...state.config.intervals.map((i) => el("option", { value: i.minutes, selected: i.minutes === s.interval_min }, i.label)),
  state.config.intervals.some((i) => i.minutes === s.interval_min) ? null
    : el("option", { value: s.interval_min, selected: true }, intervalLabel(s.interval_min)),
  el("option", { value: "custom" }, "своё…"));

  const toggle = el("label", { class: "switch", title: s.enabled ? "Поставить на паузу" : "Включить" },
    el("input", { type: "checkbox", checked: s.enabled, onchange: async (e) => {
      await api(`/api/subs/${s.id}`, { method: "PATCH", body: { enabled: e.target.checked } });
      showSubs();
    } }), el("span", {}, s.enabled ? "активна" : "пауза"));

  return el("article", { class: "sub-card" + (s.enabled ? "" : " paused") },
    el("div", { class: "sub-main" },
      el("a", { href: `#subs/${s.id}`, class: "sub-q" }, s.query, s.new ? el("span", { class: "count new" }, `+${s.new}`) : null),
      el("div", { class: "muted small" }, filterSummary(s.params), ` · сохранено ${s.total}`),
      el("div", { class: "muted small" },
        `Проверено: ${s.last_run ? ago(s.last_run) : "ещё нет"} · следующая: ${s.enabled ? when(s.next_run) : "—"}`),
      s.last_error ? el("div", { class: "err small", title: s.last_error }, "⚠ " + s.last_error.slice(0, 120)) : null),
    el("div", { class: "sub-ctrl" }, sel, toggle,
      el("button", { type: "button", class: "btn small", onclick: async (e) => {
        e.target.disabled = true;
        e.target.textContent = "Проверка…";
        const r = await api(`/api/subs/${s.id}/run`, { method: "POST" });
        toast(r.run.ok ? (r.run.new ? `Новых моделей: ${r.run.new}` : "Новых нет") : "Ошибка: " + r.run.error, 4000);
        showSubs();
      } }, "Проверить сейчас"),
      el("button", { type: "button", class: "btn small ghost danger", onclick: async () => {
        if (!confirm(`Удалить подписку «${s.query}»?`)) return;
        await api(`/api/subs/${s.id}`, { method: "DELETE" });
        showSubs();
      } }, "Удалить")));
}

export async function showSubDetail(id) {
  const d = await api(`/api/subs/${id}/items`).catch(() => null);
  if (!d || !d.sub) { location.hash = "#subs"; return; }
  $("#subs-list").hidden = true;
  $("#subs-empty").hidden = true;
  $("#sub-detail").hidden = false;
  $("#sub-title").textContent = `«${d.sub.query}» · ${d.sub.new ? `новых ${d.sub.new} из ${d.sub.total}` : `${d.sub.total} моделей`}`;
  $("#sub-grid").replaceChildren(...d.items.map((h) => renderCard({ ...h, score: null, breakdown: null }, { isNew: h.is_new })));
  $("#sub-mark-seen").hidden = !d.sub.new;
  $("#sub-mark-seen").onclick = async () => {
    await api(`/api/subs/${id}/seen`, { method: "POST" });
    showSubDetail(id);
    refreshSubsBadge();
  };
  $("#sub-open-search").onclick = () => loadIntoSearch(d.sub.query, d.sub.params);
}
