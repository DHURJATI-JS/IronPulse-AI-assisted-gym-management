/* Super-admin site overview (/superadmin/insights): "Ask the AI" without a page reload, through IP.ai (feature
 * "admin_overview" - its quota and cooldown bucket). Markup: superadmin/insights.html
 *   <section data-component="sa-insights"> <form data-insights-form> [data-insights-suggest] buttons,
 *   [data-insights-input] </form> [data-insights-wait] [data-insights-answer] </section>
 * The form works without JavaScript (the server renders the answer). Here the question goes to the same URL as JSON
 * and the answer is built with IP.h / textContent only; a link is shown only when it is a same-origin admin path
 * (the server checked it already - checked again). Refusals (quota, AI paused, duplicate, spam, too many questions)
 * are explained in place with IP.ai.describeError, and the buttons stay off while IP.ai.cooldownLeft() runs. Another
 * 4xx (an expired CSRF token ...) falls back to the normal form post.
 */
(function () {
  "use strict";
  var IP = window.IP;
  if (!IP || !IP.ai) return;
  var FEATURE = "admin_overview";
  var LINK = /^\/superadmin(?:\/[A-Za-z0-9._:\-]+)*\/?(?:\?[A-Za-z0-9._:=&%+\-]*)?(?:#[A-Za-z0-9_\-]*)?$/;
  var TONES = { critical: "red", high: "red", medium: "amber", low: "blue" };
  var LABELS = { critical: "Critical", high: "High", medium: "Medium", low: "Low" };
  var REFUSALS = { 409: true, 422: true, 429: true, 503: true };
  var MAX_ITEMS = 6;

  /** The link when it is a same-origin admin path (no scheme, host, "//", "..", backslash or space), else null. */
  function safeLink(value) {
    var s = String(value == null ? "" : value).trim();
    if (!s || s.length > 300 || s.indexOf("..") !== -1 || s.indexOf("//") !== -1 || !LINK.test(s)) return null;
    return s;
  }

  IP.register("sa-insights", function (root) {
    var form = root.querySelector("[data-insights-form]");
    var input = root.querySelector("[data-insights-input]");
    var box = root.querySelector("[data-insights-answer]");
    var waitEl = root.querySelector("[data-insights-wait]");
    if (!form || !input || !box) return;
    var buttons = Array.prototype.slice.call(form.querySelectorAll("button"));
    var busy = false;
    var ticker = null;

    function sync() {
      var wait = IP.ai.cooldownLeft(FEATURE);
      var off = busy || wait > 0;
      buttons.forEach(function (b) {
        b.disabled = off;
      });
      if (waitEl) {
        waitEl.textContent = wait > 0 ? "You can ask again in " + IP.ai.formatWait(wait) + "." : "";
        waitEl.hidden = !(wait > 0);
      }
      if (wait > 0 && !ticker) {
        ticker = setInterval(sync, 1000);
      } else if (!(wait > 0) && ticker) {
        clearInterval(ticker);
        ticker = null;
      }
    }

    function show(node) {
      while (box.firstChild) box.removeChild(box.firstChild);
      box.appendChild(node);
      box.classList.remove("hidden");
      var stale = root.querySelector("[data-insights-error]");
      if (stale) stale.parentNode.removeChild(stale);
    }

    function badge(severity) {
      var key = LABELS[severity] ? severity : "medium";
      return IP.h("span", { class: "badge badge-" + TONES[key], text: LABELS[key] });
    }

    function render(data) {
      var card = IP.h("div", { class: "rounded-2xl bg-zinc-100/80 p-4 text-[15px] leading-relaxed dark:bg-white/[0.05]",
        "data-insights-result": data.source === "ai" ? "ai" : "fallback" });
      card.appendChild(IP.h("p", { class: "text-xs font-medium muted", text: data.question || "" }));
      card.appendChild(IP.h("p", { class: "mt-1 whitespace-pre-line", text: data.overview || "" }));
      var items = Array.isArray(data.fix_first) ? data.fix_first.slice(0, MAX_ITEMS) : [];
      if (items.length) {
        var list = IP.h("ol", { class: "mt-3 grid gap-2", "aria-label": "Fix first" });
        items.forEach(function (it) {
          if (!it || typeof it !== "object") return;
          var li = IP.h("li", { class: "rounded-xl bg-white/70 px-3 py-2 dark:bg-black/20" });
          li.appendChild(IP.h("p", { class: "flex flex-wrap items-center gap-2 text-sm font-semibold" },
            badge(it.severity), IP.h("span", { text: String(it.title || "") })));
          if (it.why) li.appendChild(IP.h("p", { class: "mt-1 text-sm", text: String(it.why) }));
          var href = safeLink(it.link);
          if (href) li.appendChild(IP.h("a", { href: href, class: "mt-1 inline-flex text-sm font-medium text-accent hover:underline", text: "Open " + href }));
          list.appendChild(li);
        });
        card.appendChild(list);
      }
      if (data.note) card.appendChild(IP.h("p", { class: "mt-2 text-xs font-medium text-[#b45309] dark:text-[#fbbf24]", text: String(data.note) }));
      card.appendChild(IP.h("p", { class: "mt-2 text-xs muted",
        text: data.source === "ai" ? "Answered by " + (data.provider || "the AI") : "Automatic overview (no AI)" }));
      show(card);
    }

    function problem(message) {
      show(IP.h("div", { class: "alert alert-warning", role: "status" }, IP.h("p", { text: message })));
    }

    function thinking() {
      show(IP.h("p", { class: "flex items-center gap-2 text-sm muted" },
        IP.h("span", { class: "spinner", "aria-hidden": "true" }), "Reading the site overview…"));
    }

    form.addEventListener("submit", async function (e) {
      var submitter = e.submitter || null;
      var suggestion = submitter && submitter.hasAttribute("data-insights-suggest") ? submitter.value : "";
      var question = suggestion || String(input.value || "").trim();
      if (!suggestion && question.length < 2) {
        e.preventDefault();
        problem("Type a question of at least 2 characters, or pick a suggestion.");
        input.focus();
        return;
      }
      e.preventDefault();
      if (busy || IP.ai.cooldownLeft(FEATURE) > 0) {
        sync();
        return;
      }
      busy = true;
      sync();
      thinking();
      try {
        var body = suggestion ? { suggestion: suggestion } : { question: question };
        render(await IP.ai.request(form.action, { json: body, feature: FEATURE, timeout: 180000 }));
      } catch (err) {
        if (err && err.name === "AbortError" && err.reason === "page-hide") return; // the page is going away
        var status = (err && err.status) || 0;
        var refused = !!(err && (err.local || REFUSALS[status] || (typeof err.code === "string" && err.code)));
        if (status && status < 500 && !refused) {
          if (suggestion) input.value = suggestion;
          form.submit(); // let the server answer the normal post (e.g. an expired CSRF token)
          return;
        }
        problem(IP.ai.describeError(err));
      } finally {
        busy = false;
        sync();
      }
    });

    sync();
  });
})();
