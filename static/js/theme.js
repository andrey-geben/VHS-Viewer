(function () {
  var KEY = "vhs_theme";
  function cookieTheme() {
    var m = document.cookie.match(/(?:^|;\s*)vhs_theme=(light|dark)(?:;|$)/);
    return m ? m[1] : "";
  }
  function current() {
    var fromCookie = cookieTheme();
    if (fromCookie) return fromCookie;
    try { return localStorage.getItem("vhs-theme") || "dark"; } catch (e) { return "dark"; }
  }
  function apply(theme) {
    document.documentElement.setAttribute("data-theme", theme === "light" ? "light" : "dark");
  }
  function syncButton() {
    var btn = document.getElementById("theme-toggle");
    if (!btn) return;
    var light = document.documentElement.getAttribute("data-theme") === "light";
    btn.textContent = light ? "☀️" : "🌙";
    btn.title = light ? "Переключить на тёмную тему" : "Переключить на светлую тему";
  }
  apply(current());
  document.addEventListener("DOMContentLoaded", function () {
    var btn = document.getElementById("theme-toggle");
    if (!btn) return;
    syncButton();
    btn.addEventListener("click", function () {
      var next = document.documentElement.getAttribute("data-theme") === "light" ? "dark" : "light";
      apply(next);
      try { localStorage.setItem("vhs-theme", next); } catch (e) {}
      document.cookie = "vhs_theme=" + next + "; path=/; max-age=31536000; SameSite=Lax";
      syncButton();
    });
  });
})();
