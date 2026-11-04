import { $, $$, api, state } from "./util.js";
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
  const name = VIEWS.includes(view) ? view : "search";
  for (const v of VIEWS) $("#view-" + v).hidden = v !== name;
  $$(".tabs .tab").forEach((t) => t.classList.toggle("active", t.dataset.view === name));
  $("#search-form").classList.toggle("dim", name !== "search");
  if (name === "favorites") showFavorites();
  if (name === "subs") (arg ? showSubDetail(Number(arg)) : showSubs());
  if (name === "settings") showSettings(arg);
}

async function init() {
  applyUi();
  setOnSettingsSaved(refreshAppState);
  const [srcs, cfg] = await Promise.all([api("/api/sources"), api("/api/config")]);
  state.sources = srcs.sources;
  state.config = cfg;
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

  refreshSubsBadge();
  setInterval(refreshSubsBadge, 60_000);
}

init().catch((e) => { $("#summary").textContent = "Не удалось загрузить настройки: " + e.message; });
