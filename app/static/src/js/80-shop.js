/* IronPulse gym shop (WP-L): bag count, product gallery + lightbox, wishlist toggle, add to bag, quantity steppers,
 * streaming "Ask AI about this product" chat, checkout fulfilment switch, Cashfree checkout, print, owner helpers.
 * Strict CSP: no inline handlers; DOM is built with IP.h / textContent only (never innerHTML with data).
 */
(function () {
  "use strict";
  var IP = window.IP;
  if (!IP) return;

  var SVG_NS = "http://www.w3.org/2000/svg";
  var PATHS = {
    x: "M18 6L6 18M6 6l12 12",
    left: "M15 18l-6-6 6-6",
    right: "M9 18l6-6-6-6",
    alert: "M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0zM12 9v4M12 17h.01",
    check: "M20 6L9 17l-5-5",
    info: "M12 22a10 10 0 1 0 0-20 10 10 0 0 0 0 20zM12 16v-4M12 8h.01",
    sparkles: "M12 3l1.9 4.6L18.5 9.5l-4.6 1.9L12 16l-1.9-4.6L5.5 9.5l4.6-1.9z",
  };

  function svg(name, cls) {
    var el = document.createElementNS(SVG_NS, "svg");
    el.setAttribute("viewBox", "0 0 24 24");
    el.setAttribute("fill", "none");
    el.setAttribute("stroke", "currentColor");
    el.setAttribute("stroke-width", "1.8");
    el.setAttribute("stroke-linecap", "round");
    el.setAttribute("stroke-linejoin", "round");
    el.setAttribute("aria-hidden", "true");
    el.setAttribute("class", cls || "size-5");
    var p = document.createElementNS(SVG_NS, "path");
    p.setAttribute("d", PATHS[name] || PATHS.info);
    el.appendChild(p);
    return el;
  }

  function toast(title, body, tone, link) {
    if (IP.island && IP.island.notify) IP.island.notify({ title: title, body: body || "", tone: tone || "info", link: link || null });
  }

  function restart(el, attr) {
    el.removeAttribute(attr);
    void el.offsetWidth; // reflow so the animation plays again
    el.setAttribute(attr, "");
  }

  function signedIn() {
    return document.body.getAttribute("data-role") !== "guest";
  }

  /* ------------------------------------------------------------------ bag count */
  function setCount(n, bump) {
    document.querySelectorAll("[data-cart-count]").forEach(function (el) {
      el.textContent = String(n);
      el.hidden = !n;
      if (bump && n) restart(el, "data-bump");
    });
  }
  IP.shop = { setCount: setCount };

  IP.register("shop-cart-count", function () {
    if (!signedIn()) return;
    IP.api("/api/shop/cart/count")
      .then(function (d) {
        if (d && typeof d.count === "number") setCount(d.count, false);
      })
      .catch(function () {});
  });

  /* ------------------------------------------------------------------ gallery + lightbox */
  function lightbox(images, start, title, onChange) {
    var idx = start || 0;
    var opener = document.activeElement;
    var img = IP.h("img", { src: images[idx], alt: title || "" });
    var counter = IP.h("p", { class: "text-sm font-medium text-white/70 tabular-nums" });
    var closeBtn = IP.h("button", { type: "button", class: "shop-lightbox-btn", "aria-label": "Close the photo viewer" });
    closeBtn.appendChild(svg("x", "size-5"));
    var prev = IP.h("button", { type: "button", class: "shop-lightbox-btn", "aria-label": "Previous photo" });
    prev.appendChild(svg("left", "size-5"));
    var next = IP.h("button", { type: "button", class: "shop-lightbox-btn", "aria-label": "Next photo" });
    next.appendChild(svg("right", "size-5"));
    var top = IP.h("div", { class: "absolute inset-x-0 top-0 flex items-center justify-between gap-3 p-4 sm:p-6" },
      IP.h("p", { class: "truncate text-sm font-semibold text-white", text: title || "" }), closeBtn);
    var nav = IP.h("div", { class: "mt-5 flex items-center gap-4" }, prev, counter, next);
    var box = IP.h("div", { class: "shop-lightbox", role: "dialog", "aria-modal": "true", "aria-label": (title || "Product") + " photos" }, top, img, nav);
    if (images.length < 2) nav.hidden = true;

    function show(i) {
      idx = (i + images.length) % images.length;
      img.src = images[idx];
      counter.textContent = idx + 1 + " / " + images.length;
      if (onChange) onChange(idx);
    }
    function close() {
      document.removeEventListener("keydown", onKey);
      document.documentElement.classList.remove("overflow-hidden");
      box.remove();
      if (opener && opener.focus) opener.focus();
    }
    function onKey(e) {
      if (e.key === "Escape") close();
      else if (e.key === "ArrowLeft") show(idx - 1);
      else if (e.key === "ArrowRight") show(idx + 1);
      else if (e.key === "Tab") {
        var f = [closeBtn, prev, next].filter(function (b) { return !b.closest("[hidden]"); });
        var at = f.indexOf(document.activeElement);
        e.preventDefault();
        f[(at + (e.shiftKey ? -1 : 1) + f.length) % f.length].focus();
      }
    }
    closeBtn.addEventListener("click", close);
    prev.addEventListener("click", function () { show(idx - 1); });
    next.addEventListener("click", function () { show(idx + 1); });
    box.addEventListener("click", function (e) { if (e.target === box) close(); });
    var touchX = null;
    box.addEventListener("touchstart", function (e) { touchX = e.touches[0].clientX; }, { passive: true });
    box.addEventListener("touchend", function (e) {
      if (touchX === null) return;
      var dx = e.changedTouches[0].clientX - touchX;
      if (Math.abs(dx) > 40) show(idx + (dx < 0 ? 1 : -1));
      touchX = null;
    });
    document.addEventListener("keydown", onKey);
    document.documentElement.classList.add("overflow-hidden");
    document.body.appendChild(box);
    show(idx);
    closeBtn.focus();
  }

  IP.register("shop-gallery", function (root) {
    var images = [];
    try { images = JSON.parse(root.getAttribute("data-images") || "[]") || []; } catch (e) { images = []; }
    if (!images.length) return;
    var title = root.getAttribute("data-title") || "";
    var main = root.querySelector("[data-gallery-img]");
    var opener = root.querySelector("[data-gallery-open]");
    var thumbs = Array.prototype.slice.call(root.querySelectorAll("[data-gallery-thumb]"));
    var current = 0;
    function show(i) {
      current = (i + images.length) % images.length;
      if (main && main.getAttribute("src") !== images[current]) {
        main.src = images[current];
        if (opener) restart(opener, "data-swap");
      }
      thumbs.forEach(function (t, k) { t.setAttribute("aria-current", k === current ? "true" : "false"); });
    }
    thumbs.forEach(function (t) {
      t.addEventListener("click", function () { show(parseInt(t.getAttribute("data-index"), 10) || 0); });
    });
    if (opener) opener.addEventListener("click", function () { lightbox(images, current, title, show); });
    var startX = null;
    if (opener) {
      opener.addEventListener("touchstart", function (e) { startX = e.touches[0].clientX; }, { passive: true });
      opener.addEventListener("touchend", function (e) {
        if (startX === null) return;
        var dx = e.changedTouches[0].clientX - startX;
        if (Math.abs(dx) > 40) { e.preventDefault(); show(current + (dx < 0 ? 1 : -1)); }
        startX = null;
      });
    }
  });

  /* ------------------------------------------------------------------ wishlist toggle */
  IP.register("shop-wish", function (form) {
    form.addEventListener("submit", function (e) {
      if (!signedIn()) return; // normal POST -> server redirects to sign-in
      e.preventDefault();
      var btn = form.querySelector("button");
      btn.disabled = true;
      IP.api(form.action, { method: "POST", form: form })
        .then(function (d) {
          var on = !!d.wishlisted;
          btn.setAttribute("aria-pressed", on ? "true" : "false");
          btn.setAttribute("title", on ? "Saved" : "Save");
          restart(btn, "data-bump");
          toast(on ? "Saved to your wishlist" : "Removed from your wishlist", d.message, on ? "success" : "info", on ? "/shop/wishlist" : null);
          var card = form.closest("[data-wishlist-item]");
          if (!on && card) {
            card.classList.add("opacity-0", "scale-95", "transition", "duration-300");
            setTimeout(function () { card.remove(); }, 300);
          }
        })
        .catch(function (err) {
          if (err.status === 401 && err.data && err.data.login) { location.href = err.data.login; return; }
          toast("Couldn't update your wishlist", err.message, "danger");
        })
        .finally(function () { btn.disabled = false; });
    });
  });

  /* ------------------------------------------------------------------ add to bag */
  IP.register("shop-add", function (form) {
    form.addEventListener("submit", function (e) {
      e.preventDefault();
      var btn = form.querySelector('button[type="submit"]');
      IP.api(form.action, { method: "POST", form: form })
        .then(function (d) {
          setCount(d.count || 0, true);
          toast(d.message && d.message.indexOf("Only") === 0 ? "Bag updated" : "Added to your bag", d.message, "success", "/shop/cart");
        })
        .catch(function (err) {
          if (err.status === 401 && err.data && err.data.login) { location.href = err.data.login; return; }
          toast("Couldn't add to your bag", err.message, "danger");
        })
        .finally(function () {
          setTimeout(function () { if (btn) { btn.disabled = false; btn.removeAttribute("aria-busy"); } }, 0);
        });
    });
  });

  /* ------------------------------------------------------------------ quantity stepper */
  IP.register("shop-qty", function (box) {
    var input = box.querySelector("input");
    if (!input) return;
    var buttons = box.querySelectorAll("[data-step]");
    var timer = null;
    function bounds() { return [parseInt(input.min || "1", 10), parseInt(input.max || "20", 10)]; }
    function sync() {
      var b = bounds();
      var v = parseInt(input.value, 10) || b[0];
      buttons.forEach(function (btn) {
        var step = parseInt(btn.getAttribute("data-step"), 10);
        btn.disabled = step < 0 ? v <= b[0] : v >= b[1];
      });
    }
    function autosubmit() {
      if (!box.hasAttribute("data-autosubmit-qty") || !input.form) return;
      clearTimeout(timer);
      timer = setTimeout(function () { input.form.requestSubmit(); }, 500);
    }
    buttons.forEach(function (btn) {
      btn.addEventListener("click", function () {
        var b = bounds();
        var v = (parseInt(input.value, 10) || b[0]) + parseInt(btn.getAttribute("data-step"), 10);
        input.value = String(Math.max(b[0], Math.min(b[1], v)));
        sync();
        autosubmit();
      });
    });
    input.addEventListener("input", sync);
    input.addEventListener("change", function () {
      var b = bounds();
      var v = parseInt(input.value, 10);
      input.value = String(Math.max(b[0], Math.min(b[1], isNaN(v) ? b[0] : v)));
      sync();
      autosubmit();
    });
    sync();
  });

  /* ------------------------------------------------------------------ "Ask AI about this product" (streamed) */
  var VERDICTS = { yes: "Looks suitable for you", no: "Not suitable for you", unsure: "Check before using" };
  var ASK_FEATURE = "shop"; // the server's AI policy: the quota and cooldown bucket of product questions
  var NO_FALLBACK = { 401: true, 403: true, 404: true, 409: true, 422: true, 429: true };

  /** The JSON endpoint is a second try only when the stream itself broke: a network failure, a 5xx without a refusal
   * code, or a JSON reply where a stream was expected. Never after a refusal, a cooldown, a timeout or leaving the
   * page - that would spend the AI (and the member's quota) twice for an answer nobody may be waiting for. */
  function canFallBack(err) {
    if (!err || err.name === "AbortError") return false;
    if (err.code === "bad_stream") return true;
    if (err.code) return false; // cooldown_local, quota, cooldown, duplicate, spam, busy, cancelled ...
    var status = Number(err.status) || 0;
    if (NO_FALLBACK[status]) return false;
    return status === 0 || status >= 500;
  }

  IP.register("shop-ask", function (root) {
    var log = root.querySelector("[data-ask-log]");
    var form = root.querySelector("[data-ask-form]");
    var input = root.querySelector("[data-ask-input]");
    var send = root.querySelector("[data-ask-send]");
    var counter = root.querySelector("[data-ask-count]");
    var waitEl = root.querySelector("[data-ask-wait]");
    var ideas = Array.prototype.slice.call(root.querySelectorAll("[data-ask-idea]"));
    var streamUrl = root.getAttribute("data-stream-url");
    var jsonUrl = root.getAttribute("data-url");
    if (!log || !form || !input || !send || !streamUrl) return;
    var busy = false;
    var ticker = null;

    function scroll() { log.scrollTop = log.scrollHeight; }
    function bubble(cls, text) {
      var b = IP.h("div", { class: "shop-bubble " + cls });
      if (text) b.textContent = text;
      log.appendChild(b);
      scroll();
      return b;
    }
    function typing() {
      var hint = IP.h("span", { class: "ml-2 text-xs muted", hidden: true, text: "Still thinking…" });
      var t = IP.h("div", { class: "shop-bubble shop-bubble-ai", role: "status", "aria-label": "The assistant is typing" },
        IP.h("span", { class: "shop-typing" }, IP.h("span"), IP.h("span"), IP.h("span")), hint);
      t._hint = hint;
      log.appendChild(t);
      scroll();
      return t;
    }
    function updateCount() { if (counter) counter.textContent = String((input.value || "").length); }

    /** Send button + question ideas: off while an answer streams and while the AI cooldown runs (with a countdown
     * outside the conversation's live region). */
    function sync() {
      var wait = IP.ai.cooldownLeft(ASK_FEATURE);
      var off = busy || wait > 0;
      send.disabled = off;
      ideas.forEach(function (b) { b.disabled = off; });
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

    function renderFinal(holder, data) {
      holder.textContent = data.answer || "";
      var meta = IP.h("div", { class: "mt-3 grid gap-2 whitespace-normal" });
      var verdict = IP.h("span", { class: "shop-verdict w-fit", "data-v": data.suitable_for_you || "unsure" });
      verdict.appendChild(svg(data.suitable_for_you === "yes" ? "check" : data.suitable_for_you === "no" ? "x" : "alert", "size-3.5"));
      verdict.appendChild(document.createTextNode(VERDICTS[data.suitable_for_you] || VERDICTS.unsure));
      meta.appendChild(verdict);
      var flags = data.safety_flags || [];
      if (flags.length) {
        var ul = IP.h("ul", { class: "grid gap-1.5 rounded-2xl bg-white/70 p-3 text-[13px] leading-snug dark:bg-black/20" });
        flags.slice(0, 8).forEach(function (f) {
          var li = IP.h("li", { class: "flex gap-2" });
          var ic = svg("alert", "mt-0.5 size-3.5 shrink-0 text-[#b45309] dark:text-[#fbbf24]");
          li.appendChild(ic);
          li.appendChild(IP.h("span", { text: f }));
          ul.appendChild(li);
        });
        meta.appendChild(ul);
      }
      var sug = data.suggestions || [];
      if (sug.length) {
        var row = IP.h("div", { class: "flex flex-wrap gap-2" });
        sug.forEach(function (s) {
          var a = IP.h("a", { href: s.url, class: "shop-suggest" });
          if (s.image) a.appendChild(IP.h("img", { src: s.image, alt: "", class: "size-6 rounded-full bg-white object-contain" }));
          a.appendChild(IP.h("span", { text: s.name }));
          a.appendChild(IP.h("span", { class: "muted tabular-nums", text: s.price }));
          row.appendChild(a);
        });
        meta.appendChild(IP.h("p", { class: "text-xs font-semibold muted", text: "You could also look at" }));
        meta.appendChild(row);
      }
      var note = data.ai_available
        ? (data.provider ? "Answered by AI (" + data.provider + ")" + (data.cached ? " · instant" : "") : "Answered by AI")
        : data.busy
          ? "AI is busy right now - answer built from the product listing"
          : "AI unavailable - answer built from the product listing";
      meta.appendChild(IP.h("p", { class: "text-[11px] muted", text: note }));
      if (data.disclaimer) meta.appendChild(IP.h("p", { class: "text-[11px] muted", text: data.disclaimer }));
      holder.appendChild(meta);
      scroll();
    }

    function errorBubble(msg) {
      var b = bubble("shop-bubble-ai", "");
      b.appendChild(svg("alert", "mr-1.5 inline size-4 text-bad"));
      b.appendChild(document.createTextNode(msg));
    }

    async function ask(question) {
      question = (question || "").trim().slice(0, 500);
      if (busy || !question) return;
      busy = true;
      sync();
      bubble("shop-bubble-me", question);
      var t = typing();
      var slow = setTimeout(function () { if (t._hint) t._hint.hidden = false; }, 5000);
      var live = { bubble: null, text: "" }; // the answer streamed so far
      var result = null;
      var failure = null;
      try {
        result = await IP.ai.stream(streamUrl, {
          json: { question: question },
          feature: ASK_FEATURE,
          timeout: 40000,
          onEvent: function (evt) {
            if (evt.type !== "delta" || !evt.text) return;
            if (!live.bubble) { t.remove(); live.bubble = bubble("shop-bubble-ai", ""); }
            live.text += evt.text;
            live.bubble.textContent = live.text;
            scroll();
          },
        });
      } catch (err) {
        failure = err;
      }
      if (failure && jsonUrl && canFallBack(failure)) {
        try {
          result = { done: await IP.ai.request(jsonUrl, { json: { question: question }, feature: ASK_FEATURE }), blocked: null };
          failure = null;
        } catch (err2) {
          failure = err2;
        }
      }
      clearTimeout(slow);
      if (t.parentNode) t.remove();
      busy = false;
      sync();
      if (failure) {
        if (failure.name === "AbortError" && failure.reason === "page-hide") return; // the page is going away
        if (failure.status === 401) { location.href = "/auth/login?next=" + encodeURIComponent(location.pathname + "#ask"); return; }
        errorBubble(IP.ai.describeError(failure));
        return;
      }
      if (result.blocked) {
        // the AI guard refused (quota, cooldown, duplicate, spam ...): why, and when to ask again
        errorBubble(result.blocked.detail || IP.ai.describeError({ code: result.blocked.code, retryAfter: result.blocked.retry_after }));
        return;
      }
      if (result.done) {
        renderFinal(live.bubble || bubble("shop-bubble-ai", ""), result.done);
        return;
      }
      // the stream ended without an answer: the server stopped it (this tab or session was closed elsewhere)
      if (live.bubble) live.bubble.appendChild(IP.h("p", { class: "mt-2 text-[11px] muted", text: "The answer was interrupted - ask again for the full answer." }));
      else errorBubble("The answer was interrupted - please try again.");
    }

    form.addEventListener("submit", function (e) {
      e.preventDefault();
      var q = input.value;
      if (busy || !q.trim() || IP.ai.cooldownLeft(ASK_FEATURE) > 0) return; // keep what they typed for later
      input.value = "";
      updateCount();
      ask(q);
    });
    ideas.forEach(function (b) {
      b.addEventListener("click", function () { ask(b.textContent); });
    });
    input.addEventListener("input", updateCount);
    IP.ai.onCooldown(sync); // a cooldown started by another AI widget of this page (code "cooldown" pauses them all)
    sync();
    if (location.hash === "#ask") setTimeout(function () { input.focus(); }, 300);
  });

  /* ------------------------------------------------------------------ checkout: pickup / delivery switch */
  IP.register("shop-checkout", function (root) {
    function apply() {
      var checked = root.querySelector('input[name="fulfillment"]:checked');
      var mode = checked ? checked.value : "pickup";
      root.querySelectorAll("[data-show-for]").forEach(function (el) {
        var on = el.getAttribute("data-show-for") === mode;
        el.hidden = !on;
        el.querySelectorAll("input, select, textarea").forEach(function (f) {
          if (f.hasAttribute("data-required")) f.required = on;
          f.disabled = !on;
        });
      });
    }
    root.querySelectorAll('input[name="fulfillment"]').forEach(function (r) { r.addEventListener("change", apply); });
    apply();
  });

  /* ------------------------------------------------------------------ Cashfree checkout (JS SDK v3) */
  IP.register("shop-cashfree", function (root) {
    var status = root.querySelector("[data-cf-status]");
    var launch = root.querySelector("[data-cf-launch]");
    var tries = 0;
    function say(msg) { if (status) status.textContent = msg; }
    function open() {
      if (typeof window.Cashfree !== "function") {
        if (tries++ < 40) { setTimeout(open, 250); return; }
        say("The secure payment window couldn't load. Check your connection and try again.");
        return;
      }
      try {
        var cf = window.Cashfree({ mode: root.getAttribute("data-mode") || "sandbox" });
        say("Opening the secure payment window…");
        cf.checkout({ paymentSessionId: root.getAttribute("data-session-id"), redirectTarget: "_self" });
      } catch (e) {
        say("The payment window couldn't open. Please try again.");
      }
    }
    if (launch) launch.addEventListener("click", function () { tries = 0; open(); });
    open();
  });

  /* ------------------------------------------------------------------ print receipt */
  IP.register("shop-print", function (btn) {
    btn.addEventListener("click", function () { window.print(); });
  });

  /* ------------------------------------------------------------------ owner: image picker preview */
  IP.register("shop-image-preview", function (input) {
    var target = document.getElementById(input.getAttribute("data-preview-target") || "");
    var max = parseInt(input.getAttribute("data-max") || "8", 10);
    if (!target) return;
    var urls = [];
    input.addEventListener("change", function () {
      urls.forEach(function (u) { URL.revokeObjectURL(u); });
      urls = [];
      while (target.firstChild) target.removeChild(target.firstChild);
      var files = Array.prototype.slice.call(input.files || []);
      if (files.length > max) {
        target.appendChild(IP.h("p", { class: "col-span-full text-xs font-medium text-bad", text: "Only " + max + " photos can be added - the first " + max + " will be used." }));
      }
      files.slice(0, max).forEach(function (f) {
        if (!/^image\//.test(f.type)) return;
        var u = URL.createObjectURL(f);
        urls.push(u);
        target.appendChild(IP.h("div", { class: "shop-img-tile" }, IP.h("img", { src: u, alt: f.name })));
      });
    });
  });

  /* ------------------------------------------------------------------ owner: coupon kind (percent vs amount) */
  IP.register("shop-coupon-kind", function (root) {
    function apply() {
      var kind = (root.querySelector('input[name="kind"]:checked') || {}).value || "percent";
      root.querySelectorAll("[data-kind]").forEach(function (el) {
        var on = el.getAttribute("data-kind") === kind;
        el.hidden = !on;
        el.querySelectorAll("input").forEach(function (f) { f.disabled = !on; });
      });
    }
    root.querySelectorAll('input[name="kind"]').forEach(function (r) { r.addEventListener("change", apply); });
    apply();
  });

  /* ------------------------------------------------------------------ owner: refund amount preview */
  IP.register("shop-refund-calc", function (root) {
    var out = root.querySelector("[data-refund-total]");
    var currency = root.getAttribute("data-currency") || "";
    if (!out) return;
    function fmt(n) {
      try { return new Intl.NumberFormat(undefined, { style: "currency", currency: currency }).format(n); } catch (e) { return n.toFixed(2); }
    }
    function calc() {
      var total = 0;
      root.querySelectorAll("[data-unit]").forEach(function (inp) {
        var q = Math.max(0, Math.min(parseInt(inp.value, 10) || 0, parseInt(inp.max || "0", 10)));
        total += q * parseFloat(inp.getAttribute("data-unit") || "0");
      });
      out.textContent = "≈ " + fmt(total);
    }
    root.addEventListener("input", calc);
    calc();
  });

  /* ------------------------------------------------------------------ catalogue: store typeahead
   * The store filter lists only the stores with the most products; this finds the others through
   * GET /api/shop/stores?q= (at most 20, debounced). Each result is a link that keeps the other filters
   * (data-base) and adds gym=<id>. Answers that arrive after a newer keystroke are dropped. */
  IP.register("shop-store-search", function (root) {
    var input = root.querySelector("[data-store-q]");
    var list = root.querySelector("[data-store-results]");
    var hint = root.querySelector("[data-store-hint]");
    var url = root.getAttribute("data-url") || "/api/shop/stores";
    var base = root.getAttribute("data-base") || "/shop";
    var MIN = 2;
    var timer = null;
    var seq = 0;
    if (!input || !list) return;

    function href(id) {
      return base + (base.indexOf("?") === -1 ? "?" : "&") + "gym=" + encodeURIComponent(id);
    }
    function say(text) {
      if (!hint) return;
      hint.textContent = text;
      hint.hidden = !text;
    }
    function show(stores, q) {
      list.textContent = "";
      stores.forEach(function (s) {
        list.appendChild(IP.h("li", null, IP.h("a", {
          href: href(s.id),
          class: "flex items-center gap-2 rounded-xl px-3 py-2 hover:bg-zinc-900/5 dark:hover:bg-white/10",
          text: s.name,
        })));
      });
      say(stores.length ? "" : "No store matches “" + q + "”.");
    }
    function search() {
      var q = input.value.replace(/\s+/g, " ").trim();
      var mine = ++seq;
      if (q.length < MIN) {
        list.textContent = "";
        say("Type " + MIN + " or more letters of the store's name.");
        return;
      }
      IP.api(url + "?q=" + encodeURIComponent(q.slice(0, 60)))
        .then(function (data) {
          if (mine === seq) show((data && data.stores) || [], q);
        })
        .catch(function () {
          if (mine === seq) say("Couldn't search the stores right now - try again.");
        });
    }
    input.addEventListener("input", function () {
      clearTimeout(timer);
      timer = setTimeout(search, 200);
    });
    root.addEventListener("toggle", function () {
      if (root.open) input.focus();
    });
  });
})();
