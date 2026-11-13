/* Light / dark / system theme toggle (persisted in localStorage "ip-theme"; initial paint handled in <head>). */
(function () {
  "use strict";
  var ORDER = ["light", "dark", "system"];
  var media = window.matchMedia ? window.matchMedia("(prefers-color-scheme: dark)") : null;

  function current() {
    try {
      return localStorage.getItem("ip-theme") || "system";
    } catch (e) {
      return "system";
    }
  }

  function apply(theme) {
    var dark = theme === "dark" || (theme === "system" && media && media.matches);
    document.documentElement.classList.toggle("dark", !!dark);
    document.documentElement.setAttribute("data-theme", theme);
    document.querySelectorAll("[data-theme-icon]").forEach(function (i) {
      if (i.getAttribute("data-theme-icon") === theme) i.removeAttribute("hidden");
      else i.setAttribute("hidden", "");
    });
  }

  document.addEventListener("DOMContentLoaded", function () {
    apply(current());
    document.querySelectorAll("[data-theme-toggle]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        var next = ORDER[(ORDER.indexOf(current()) + 1) % ORDER.length];
        try {
          localStorage.setItem("ip-theme", next);
        } catch (e) {}
        apply(next);
        if (window.IP && IP.island) IP.island.notify({ title: "Theme: " + next, tone: "info", timeout: 1400 });
      });
    });
    if (media && media.addEventListener) {
      media.addEventListener("change", function () {
        if (current() === "system") apply("system");
      });
    }
  });
})();
