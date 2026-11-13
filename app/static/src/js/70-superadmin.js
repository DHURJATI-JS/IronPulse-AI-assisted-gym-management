/* WP-G super-admin console behaviours (strict CSP: no inline scripts, DOM built with IP.h / textContent).
 *
 *  sa-bars     widths of ranked / segmented bars from data-pct (animated on first paint)
 *  sa-chart    hover / touch tooltips for the 24-hour column chart (SVG rects with data-tip)
 *  sa-busy     form submit -> button shows its data-busy-label while the request runs
 *  sa-ai-test  "Test providers" without a page reload, through IP.ai (feature "selftest"): AI-layer refusals
 *              (queue full, too many requests, a local cooldown) are explained in place with IP.ai.describeError;
 *              other 4xx answers fall back to the normal POST + flash
 *  sa-preview  graceful fallback when a private document / ID image cannot be loaded
 */
(function () {
  "use strict";
  if (!window.IP) return;
  var IP = window.IP;

  function pct(value) {
    var n = parseFloat(value);
    if (!isFinite(n) || n < 0) n = 0;
    return Math.min(100, n);
  }

  /* ------------------------------------------------------------------ bars */
  IP.register("sa-bars", function (el) {
    var spans = el.matches("[data-pct]") ? [el] : [];
    el.querySelectorAll("[data-pct]").forEach(function (s) { spans.push(s); });
    var apply = function () {
      spans.forEach(function (s) {
        var p = pct(s.getAttribute("data-pct"));
        s.style.width = (p > 0 && p < 1.5 ? 1.5 : p) + "%";
      });
    };
    if (window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches) apply();
    else window.requestAnimationFrame(function () { window.requestAnimationFrame(apply); });
  });

  /* ------------------------------------------------------------------ column chart tooltip */
  IP.register("sa-chart", function (el) {
    var svg = el.querySelector("svg");
    if (!svg) return;
    var tip = IP.h("div", { class: "sa-tooltip", role: "tooltip", hidden: true });
    el.appendChild(tip);
    var active = null;

    function clear() {
      tip.hidden = true;
      if (active) active.removeAttribute("data-active");
      el.removeAttribute("data-hover");
      active = null;
    }

    function show(hit) {
      var idx = hit.getAttribute("data-bar");
      var bar = svg.querySelector('rect[data-bar="' + idx + '"]:not(.sa-hit)');
      if (active && active !== bar) active.removeAttribute("data-active");
      active = bar;
      if (bar) bar.setAttribute("data-active", "");
      el.setAttribute("data-hover", "");
      tip.textContent = hit.getAttribute("data-tip") || "";
      tip.hidden = false;
      var box = el.getBoundingClientRect();
      var r = (bar || hit).getBoundingClientRect();
      var x = r.left - box.left + r.width / 2;
      var half = tip.offsetWidth / 2;
      x = Math.max(half, Math.min(box.width - half, x));
      tip.style.left = x + "px";
      tip.style.top = Math.max(0, r.top - box.top - 8) + "px";
    }

    svg.addEventListener("pointermove", function (e) {
      var hit = e.target.closest && e.target.closest("[data-tip]");
      if (hit) show(hit);
    });
    svg.addEventListener("pointerdown", function (e) {
      var hit = e.target.closest && e.target.closest("[data-tip]");
      if (hit) show(hit);
    });
    svg.addEventListener("pointerleave", clear);
    document.addEventListener("scroll", clear, { passive: true });
  });

  /* ------------------------------------------------------------------ busy labels */
  function busy(form) {
    var btn = form.querySelector("button[data-busy-label]");
    if (!btn || btn.getAttribute("data-busy") === "1") return;
    btn.setAttribute("data-busy", "1");
    btn.setAttribute("data-label", btn.textContent.trim());
    btn.textContent = "";
    btn.appendChild(IP.h("span", { class: "spinner", "aria-hidden": "true" }));
    btn.appendChild(document.createTextNode(" " + btn.getAttribute("data-busy-label")));
  }

  IP.register("sa-busy", function (form) {
    form.addEventListener("submit", function (e) {
      window.setTimeout(function () {
        if (!e.defaultPrevented) busy(form);
      }, 0);
    });
  });

  // Any super-admin form whose submit button declares data-busy-label (decision panel, ban form ...)
  document.addEventListener("submit", function (e) {
    var form = e.target;
    if (!form || !form.closest || !form.closest("main") || !document.querySelector(".sa-subnav")) return;
    if (form.hasAttribute("data-component")) return; // components handle themselves
    window.setTimeout(function () {
      if (!e.defaultPrevented) busy(form);
    }, 0);
  });

  /* ------------------------------------------------------------------ AI provider test */
  // answers of the AI layer that carry a reason (AI queue full, too many requests, a local cooldown): shown in place
  var AI_REFUSALS = { 409: true, 422: true, 429: true, 503: true };

  function describeAiError(err) {
    if (IP.ai && typeof IP.ai.describeError === "function") return IP.ai.describeError(err);
    return (err && err.message) || "The test request failed.";
  }

  IP.register("sa-ai-test", function (form) {
    var box = document.querySelector("[data-ai-test-result]");
    var btn = form.querySelector("button[type=submit]");
    if (!box || !btn) return;
    var original = Array.prototype.map.call(btn.childNodes, function (n) { return n.cloneNode(true); });

    function row(name, value) {
      return IP.h("div", { class: "flex justify-between gap-3" },
        IP.h("dt", { class: "opacity-75", text: name }), IP.h("dd", { class: "font-medium text-right", text: value }));
    }

    function send() {
      if (IP.ai && typeof IP.ai.request === "function") return IP.ai.request(form.action, { form: form, feature: "selftest" });
      return IP.api(form.action, { method: "POST", form: form });
    }

    function render(data) {
      while (box.firstChild) box.removeChild(box.firstChild);
      var ok = !!data.ok;
      var card = IP.h("section", { class: "sa-ai-result " + (ok ? "sa-ai-result-ok" : "sa-ai-result-bad"), "aria-label": "Provider test result" });
      card.appendChild(IP.h("p", { class: "text-lg font-semibold",
        text: ok ? (data.label || data.provider) + " answered in " + data.latency_ms + " ms" : data.title || "AI unavailable - every provider failed" }));
      card.appendChild(IP.h("p", { class: "mt-1 text-sm",
        text: ok ? "Reply: “" + (data.reply || "") + "” · model " + (data.model || "?") + " · whole test " + data.total_ms + " ms"
                 : (data.error || "No provider answered.") }));
      var list = IP.h("dl", { class: "mt-4 grid gap-1.5 text-sm sm:grid-cols-3 sm:gap-4" });
      (data.providers || []).forEach(function (p) {
        var state = { up: "online", down: "offline", unknown: "not checked", not_configured: "not configured" }[p.status] || p.status;
        var cell = IP.h("div", { class: "rounded-2xl bg-white/60 px-3 py-2 dark:bg-black/20" });
        cell.appendChild(row(p.label, state));
        if (p.error && p.status !== "up") cell.appendChild(IP.h("p", { class: "mt-1 truncate text-xs opacity-75", text: p.error, title: p.error }));
        list.appendChild(cell);
      });
      card.appendChild(list);
      if (data.disclaimer) card.appendChild(IP.h("p", { class: "mt-4 text-xs opacity-75", text: data.disclaimer }));
      box.appendChild(card);
      box.classList.remove("hidden");
      if (IP.island) {
        IP.island.notify({ title: ok ? "AI test passed" : data.refused ? "AI test not run" : "AI test failed",
          body: ok ? (data.label || "") + " · " + data.latency_ms + " ms" : data.refused ? data.error || "" : "All providers failed",
          tone: ok ? "success" : data.refused ? "warning" : "danger", timeout: 3500 });
      }
    }

    form.addEventListener("submit", async function (e) {
      e.preventDefault();
      if (btn.disabled) return;
      btn.disabled = true;
      btn.setAttribute("aria-busy", "true");
      btn.textContent = "";
      btn.appendChild(IP.h("span", { class: "spinner", "aria-hidden": "true" }));
      btn.appendChild(document.createTextNode(" " + (btn.getAttribute("data-busy-label") || "Testing…")));
      try {
        render(await send());
      } catch (err) {
        if (err && err.name === "AbortError" && err.reason === "page-hide") return; // the page is going away
        var status = (err && err.status) || 0;
        var refused = !!(err && (err.local || AI_REFUSALS[status] || (typeof err.code === "string" && err.code)));
        if (status && status < 500 && !refused) {
          form.submit(); // let the server render its normal response (e.g. expired CSRF token)
          return;
        }
        render({ ok: false, refused: refused, title: refused ? "The provider test did not run" : "The provider test failed",
                 error: describeAiError(err) });
      } finally {
        btn.disabled = false;
        btn.removeAttribute("aria-busy");
        while (btn.firstChild) btn.removeChild(btn.firstChild);
        original.forEach(function (n) { btn.appendChild(n.cloneNode(true)); });
      }
    });
  });

  /* ------------------------------------------------------------------ previews */
  IP.register("sa-preview", function (el) {
    var img = el.querySelector("img");
    var fallback = el.querySelector(".sa-preview-fallback");
    if (!img || !fallback) return;
    function broken() {
      el.setAttribute("data-broken", "");
      fallback.hidden = false;
    }
    if (img.complete && img.naturalWidth === 0 && img.getAttribute("src")) broken();
    img.addEventListener("error", broken);
  });
})();
