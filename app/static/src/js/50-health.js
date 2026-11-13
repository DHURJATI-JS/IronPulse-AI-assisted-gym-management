/* WP-E1 health UI (strict CSP: no inline handlers, DOM built with IP.h / textContent only).
 *   data-component="bedtime"       live bedtime countdown + day progress (Today dashboard)
 *   data-component="bmi-preview"   live BMI while typing height/weight (metrics forms)
 *   data-component="trend-chart"   crosshair + tooltip + keyboard reading for the weight trend
 *   data-component="photo-preview" local preview of a chosen photo (never uploaded until submit)
 *   data-component="symptoms"      "None of these" is exclusive; red-flag symptoms reveal an emergency note
 */
(function () {
  "use strict";
  var IP = window.IP;
  if (!IP) return;

  var MINUTE = 60000;
  var DAY = 86400000;

  function duration(ms) {
    var mins = Math.max(0, Math.round(ms / MINUTE));
    var h = Math.floor(mins / 60);
    var m = mins % 60;
    if (h && m) return h + " h " + m + " min";
    if (h) return h + " h";
    return m + " min";
  }

  /* ------------------------------------------------------------------ bedtime countdown */
  IP.register("bedtime", function (el) {
    var bed = Date.parse(el.getAttribute("data-bedtime-at"));
    var wake = Date.parse(el.getAttribute("data-wake-at"));
    var dayStart = Date.parse(el.getAttribute("data-day-start"));
    var out = el.querySelector("[data-bedtime-countdown]");
    var badge = el.querySelector("[data-bedtime-state]");
    var bar = el.querySelector("[data-bedtime-progress]");
    if (isNaN(bed) || isNaN(wake) || !out) return;

    function tick() {
      var now = Date.now();
      while (now >= wake) { // the page stayed open past wake-up: roll over to tonight's window
        bed += DAY;
        wake += DAY;
        dayStart += DAY;
      }
      var state, label;
      if (now >= bed) {
        state = "sleep";
        label = "Past your bedtime - time to sleep";
      } else if (bed - now <= 30 * MINUTE) {
        state = "wind-down";
        label = "Wind down - bedtime in " + duration(bed - now);
      } else {
        state = "awake";
        label = "in " + duration(bed - now);
      }
      out.textContent = label;
      if (badge) badge.textContent = state === "sleep" ? "Sleep" : state === "wind-down" ? "Wind down" : "Awake";
      el.setAttribute("data-state", state);
      if (bar && !isNaN(dayStart) && bed > dayStart) {
        var pct = state === "sleep" ? 100 : Math.min(100, Math.max(0, ((now - dayStart) / (bed - dayStart)) * 100));
        bar.setAttribute("width", pct.toFixed(1));
      }
    }
    tick();
    setInterval(tick, 30000);
  });

  /* ------------------------------------------------------------------ live BMI preview */
  IP.register("bmi-preview", function (form) {
    var h = form.querySelector('[name="height_cm"]');
    var w = form.querySelector('[name="weight_kg"]');
    var out = form.querySelector("[data-bmi-out]");
    var minor = form.getAttribute("data-minor") === "1";
    if (!h || !w || !out) return;

    function num(input) {
      var v = parseFloat(String(input.value || "").replace(",", "."));
      return isFinite(v) ? v : NaN;
    }
    function category(b) {
      if (b < 18.5) return "below the healthy range";
      if (b < 25) return "healthy range";
      if (b < 30) return "above the healthy range";
      return "obesity range";
    }
    function update() {
      var hv = num(h);
      var wv = num(w);
      if (!(hv >= 50 && hv <= 250 && wv >= 20 && wv <= 300)) {
        out.textContent = "Enter height (50-250 cm) and weight (20-300 kg) to see your BMI.";
        return;
      }
      var b = Math.round((wv / Math.pow(hv / 100, 2)) * 10) / 10;
      out.textContent = "BMI " + b.toFixed(1) + (minor ? "" : " · " + category(b));
    }
    h.addEventListener("input", update);
    w.addEventListener("input", update);
  });

  /* ------------------------------------------------------------------ trend chart hover / keyboard layer */
  IP.register("trend-chart", function (fig) {
    var plot = fig.querySelector("[data-plot]");
    var cross = fig.querySelector("[data-crosshair]");
    var tip = fig.querySelector("[data-tooltip]");
    var live = fig.querySelector("[data-live]");
    var pts;
    try {
      pts = JSON.parse(fig.getAttribute("data-points") || "[]");
    } catch (e) {
      pts = [];
    }
    if (!plot || !cross || !tip || !pts.length) return;
    var unit = fig.getAttribute("data-unit") || "";
    var idx = pts.length - 1;
    var dot = IP.h("span", {
      class: "pointer-events-none absolute size-3 -translate-x-1/2 -translate-y-1/2 rounded-full bg-accent ring-2 ring-white dark:ring-surface-dark",
      hidden: true,
      "aria-hidden": "true",
    });
    plot.appendChild(dot);

    function show(i) {
      idx = Math.max(0, Math.min(pts.length - 1, i));
      var p = pts[idx];
      var left = p.x * 100 + "%";
      cross.style.left = left;
      dot.style.left = left;
      dot.style.top = p.y * 100 + "%";
      cross.hidden = false;
      dot.hidden = false;
      tip.textContent = "";
      tip.appendChild(IP.h("strong", { class: "block text-sm font-semibold tabular-nums", text: Number(p.v).toFixed(1) + " " + unit }));
      tip.appendChild(IP.h("span", { class: "block text-xs text-zinc-500 dark:text-zinc-400", text: p.d }));
      tip.hidden = false;
      var width = plot.clientWidth;
      var tw = tip.offsetWidth;
      tip.style.left = Math.max(0, Math.min(width - tw, p.x * width - tw / 2)) + "px";
      if (live) live.textContent = p.d + ": " + Number(p.v).toFixed(1) + " " + unit;
    }
    function hide() {
      cross.hidden = true;
      dot.hidden = true;
      tip.hidden = true;
    }
    function nearest(clientX) {
      var r = plot.getBoundingClientRect();
      var x = (clientX - r.left) / (r.width || 1);
      var best = 0;
      var bestD = Infinity;
      for (var i = 0; i < pts.length; i++) {
        var d = Math.abs(pts[i].x - x);
        if (d < bestD) {
          bestD = d;
          best = i;
        }
      }
      return best;
    }
    plot.addEventListener("pointermove", function (e) { show(nearest(e.clientX)); });
    plot.addEventListener("pointerdown", function (e) { show(nearest(e.clientX)); });
    plot.addEventListener("pointerleave", function () {
      if (document.activeElement !== plot) hide();
    });
    plot.addEventListener("focus", function () { show(idx); });
    plot.addEventListener("blur", hide);
    plot.addEventListener("keydown", function (e) {
      var handled = true;
      if (e.key === "ArrowLeft") show(idx - 1);
      else if (e.key === "ArrowRight") show(idx + 1);
      else if (e.key === "Home") show(0);
      else if (e.key === "End") show(pts.length - 1);
      else if (e.key === "Escape") hide();
      else handled = false;
      if (handled) e.preventDefault();
    });
  });

  /* ------------------------------------------------------------------ local photo preview */
  IP.register("photo-preview", function (wrap) {
    var input = wrap.querySelector('input[type="file"]');
    var box = wrap.querySelector("[data-preview]");
    var img = wrap.querySelector("[data-preview-img]");
    var name = wrap.querySelector("[data-preview-name]");
    var clear = wrap.querySelector("[data-preview-clear]");
    var url = null;
    if (!input || !box || !img) return;

    function reset() {
      if (url) URL.revokeObjectURL(url);
      url = null;
      img.removeAttribute("src");
      box.hidden = true;
    }
    input.addEventListener("change", function () {
      reset();
      var f = input.files && input.files[0];
      if (!f) return;
      if (name) name.textContent = f.name + " · " + Math.max(1, Math.round(f.size / 1024)) + " KB";
      if (/^image\//.test(f.type)) {
        url = URL.createObjectURL(f);
        img.src = url;
        img.hidden = false;
      } else {
        img.hidden = true;
      }
      box.hidden = false;
    });
    if (clear) {
      clear.addEventListener("click", function (e) {
        e.preventDefault();
        input.value = "";
        reset();
        input.focus();
      });
    }
  });

  /* ------------------------------------------------------------------ symptoms: exclusive "none" + red-flag note */
  IP.register("symptoms", function (section) {
    var boxes = Array.prototype.slice.call(section.querySelectorAll('input[type="checkbox"][name="symptoms"]'));
    var note = section.querySelector("[data-red-flag-note]");
    function refresh() {
      if (!note) return;
      note.hidden = !boxes.some(function (b) { return b.checked && b.hasAttribute("data-red-flag"); });
    }
    section.addEventListener("change", function (e) {
      var t = e.target;
      if (!t || t.name !== "symptoms") return;
      if (t.checked) {
        boxes.forEach(function (b) {
          if (b !== t && (t.value === "none" || b.value === "none")) b.checked = false;
        });
      }
      refresh();
    });
    refresh();
  });
})();
