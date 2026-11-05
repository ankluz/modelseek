(function () {
  var ui = {};
  var guest = document.documentElement.dataset.profile === "public";
  try {
    if (guest) {
      var m = document.cookie.split("; ").filter(function (c) { return c.indexOf("ms_guest_ui=") === 0; })[0];
      ui = m ? JSON.parse(decodeURIComponent(m.slice(12))) : {};
    } else ui = JSON.parse(localStorage.getItem("modelseek.ui") || "{}");
  } catch (e) {}
  ui = ui || {};
  var t = ui.theme || "auto";
  if (t === "auto") t = matchMedia("(prefers-color-scheme: light)").matches ? "latte" : "plate";
  document.documentElement.dataset.theme = t;
  document.documentElement.dataset.density = ui.density || "normal";
})();
