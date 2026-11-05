import { $, $$, GUEST, api, state } from "./util.js";
import { bindSettings, loadPresets, loadSettings, setOnChange, syncForm } from "./settings.js";
import { bindSearch, renderChips, rerunSoon, search } from "./search.js";
import { loadFavorites, showFavorites } from "./favorites.js";
import { refreshSubsBadge, showSubDetail, showSubs } from "./subs.js";
import { applyUi, setOnSettingsSaved, showSettings } from "./appsettings.js";
import { bindImageSearch } from "./imagesearch.js";

const VIEWS = ["search", "favorites", "subs", "settings"];

async function refreshAppState() {
  const [srcs, cfg] = await Promise.all([api("/api/sources"), api("/api/config")]);
  state.sources = srcs.sources;
  state.config = cfg;
  loadSettings();
  syncForm();
  renderChips();
}

function route() {
  const hash = location.hash || "#search";
  const [view, arg] = hash.slice(1).split("/");
  const name = VIEWS.includes(view) && !(GUEST && view === "subs") ? view : "search";
  for (const v of VIEWS) $("#view-" + v).hidden = v !== name;
  $$(".tabs .tab").forEach((t) => t.classList.toggle("active", t.dataset.view === name));
  $("#search-form").classList.toggle("dim", name !== "search");
  if (name === "favorites") showFavorites();
  if (name === "subs") (arg ? showSubDetail(Number(arg)) : showSubs());
  if (name === "settings") showSettings(arg);
}

// A guest behind the shared password logs in before anything else loads.
function login() {
  const dlg = $("#login-dialog"), form = $("#login-form"), errBox = $("#login-error");
  return new Promise((resolve) => {
    form.onsubmit = async (e) => {
      e.preventDefault();
      errBox.hidden = true;
      try {
        await api("/api/login", { method: "POST", body: { password: $("#login-password").value } });
        dlg.close();
        resolve();
      } catch (err) { errBox.textContent = err.message; errBox.hidden = false; }
    };
    dlg.addEventListener("cancel", (e) => e.preventDefault());
    if (!dlg.open) dlg.showModal();
    $("#login-password").focus();
  });
}

async function ensureSession() {
  const s = await api("/api/session");
  if (!s.logged_in) await login();
}

async function init() {
  applyUi();
  setOnSettingsSaved(refreshAppState);
  if (GUEST) {
    await ensureSession();
    document.addEventListener("need-login", () => { login().then(() => location.reload()); });
  }
  const [srcs, cfg] = await Promise.all([api("/api/sources"), api("/api/config")]);
  state.sources = srcs.sources;
  state.config = cfg;
  if (cfg.source_url) $("#source-link").href = cfg.source_url;
  loadSettings();
  await Promise.all([loadFavorites(), loadPresets()]);

  setOnChange(rerunSoon);
  bindSettings();
  bindSearch();
  bindImageSearch();
  syncForm();
  renderChips();

  document.addEventListener("favorites-changed", () => {
    if (location.hash.startsWith("#favorites")) showFavorites();
  });
  window.addEventListener("hashchange", route);
  route();

  const q = new URLSearchParams(location.search).get("q");
  if (q) { $("#q").value = q; search(q); }
  else if (!location.hash || location.hash === "#search") $("#q").focus();

  if (!GUEST) {
    refreshSubsBadge();
    setInterval(refreshSubsBadge, 60_000);
  }
}

init().catch((e) => { $("#summary").textContent = "Не удалось загрузить настройки: " + e.message; });
