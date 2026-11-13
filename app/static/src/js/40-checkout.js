/* WP-D plans, checkout and front desk components (strict CSP: no inline scripts, DOM built with IP.h).
 *   plan-picker        subscribe page: live summary, branch section toggle, "Use my location" -> re-check distances
 *   presence-form      desk check: attaches GPS automatically when permission was already granted
 *   otp-input          6-digit code field (digits only)
 *   countdown          mm:ss until data-expires (ISO); data-reload="1" emits "countdown.done" at zero
 *   desk-live          owner desk: refreshes when a desk notification arrives over the WebSocket
 *   cashfree-checkout  opens Cashfree's JS SDK checkout (redirectTarget "_self")
 *   plan-editor        owner plans: unsaved-changes state
 *   manual-sale        owner desk: branch field only for gym/combo plans, branches filtered by gym
 */
(function () {
  "use strict";
  var IP = window.IP;

  function toast(title, body, tone) {
    if (IP.island) IP.island.notify({ title: title, body: body, tone: tone || "info", timeout: 3200 });
  }

  /* ---------------------------------------------------------------- subscribe: plan picker */
  IP.register("plan-picker", function (root) {
    var form = root.querySelector("[data-plan-form]");
    if (!form) return;
    var branchSection = root.querySelector("[data-branch-section]");
    var deskNote = root.querySelector("[data-desk-note]");
    var bar = root.querySelector("[data-mobile-bar]");
    var summaryCard = root.querySelector("[data-summary-card]");
    var summaryInView = false;

    function each(selector, fn) {
      root.querySelectorAll(selector).forEach(fn);
    }

    function selectedPlan() {
      return form.querySelector('input[name="plan"]:checked');
    }

    function sync() {
      var input = selectedPlan();
      var card = input ? input.closest("[data-plan-option]") : null;
      if (card) {
        each("[data-summary-name]", function (el) { el.textContent = card.getAttribute("data-name"); });
        each("[data-summary-mode]", function (el) {
          el.textContent = card.getAttribute("data-mode-label") + " · " + card.getAttribute("data-duration") + " days";
        });
        each("[data-summary-price]", function (el) { el.textContent = card.getAttribute("data-price"); });
      }
      var mode = card ? card.getAttribute("data-mode") : "";
      var needsBranch = mode === "offline" || mode === "combo";
      if (branchSection) {
        branchSection.hidden = !!card && !needsBranch;
        branchSection.querySelectorAll('input[name="branch_id"]').forEach(function (r) {
          r.required = needsBranch;
        });
      }
      if (deskNote) deskNote.hidden = !!card && !needsBranch;
      each("[data-summary-submit]", function (btn) { btn.disabled = !card; });
      if (bar) bar.hidden = !card || summaryInView;
    }

    // the sticky phone bar steps aside while the full summary card (with its own button) is on screen
    if (bar && summaryCard && "IntersectionObserver" in window) {
      new IntersectionObserver(function (entries) {
        summaryInView = entries[0].isIntersecting;
        sync();
      }).observe(summaryCard);
    }

    form.addEventListener("change", function (e) {
      if (e.target && e.target.name === "plan") sync();
    });
    sync();

    // "Use my location" (geolocate component) -> re-render server side with exact distances
    IP.on("location", function (loc) {
      if (!loc || typeof loc.lat !== "number") return;
      var url = new URL(root.getAttribute("data-base-url") || location.pathname, location.origin);
      url.searchParams.set("lat", String(loc.lat));
      url.searchParams.set("lon", String(loc.lon));
      var plan = selectedPlan();
      if (plan) url.searchParams.set("plan", plan.value);
      var branch = form.querySelector('input[name="branch_id"]:checked');
      if (branch) url.searchParams.set("branch", branch.value);
      setTimeout(function () {
        location.assign(url.pathname + url.search);
      }, 450);
    });
  });

  /* ---------------------------------------------------------------- desk check: presence form */
  IP.register("presence-form", function (form) {
    var lat = form.querySelector('input[name="lat"]');
    var lon = form.querySelector('input[name="lon"]');
    var badge = form.querySelector("[data-location-attached]");

    function attached() {
      if (!badge) return;
      badge.classList.remove("hidden");
      badge.classList.add("flex");
    }

    IP.on("location", function (loc) {
      if (!loc || !lat || !lon) return;
      lat.value = loc.lat;
      lon.value = loc.lon;
      attached();
    });

    form.addEventListener("submit", function (e) {
      if (!lat || !lon || lat.value || !navigator.geolocation || !navigator.permissions || form.__ipLocating) return;
      // permission already granted -> attach the location silently, then submit
      e.preventDefault();
      form.__ipLocating = true;
      var sent = false;
      function go() {
        if (sent) return;
        sent = true;
        form.submit();
      }
      setTimeout(go, 5000);
      navigator.permissions
        .query({ name: "geolocation" })
        .then(function (status) {
          if (status.state !== "granted") return go();
          navigator.geolocation.getCurrentPosition(
            function (pos) {
              lat.value = Math.round(pos.coords.latitude * 1e6) / 1e6;
              lon.value = Math.round(pos.coords.longitude * 1e6) / 1e6;
              attached();
              go();
            },
            go,
            { enableHighAccuracy: true, timeout: 4000, maximumAge: 60000 }
          );
        })
        .catch(go);
    });
  });

  /* ---------------------------------------------------------------- 6-digit code */
  IP.register("otp-input", function (form) {
    var input = form.querySelector('input[name="code"]');
    if (!input) return;
    input.addEventListener("input", function () {
      var clean = input.value.replace(/\D/g, "").slice(0, 6);
      if (clean !== input.value) input.value = clean;
    });
    input.addEventListener("paste", function (e) {
      var text = (e.clipboardData || window.clipboardData).getData("text") || "";
      var digits = text.replace(/\D/g, "").slice(0, 6);
      if (digits) {
        e.preventDefault();
        input.value = digits;
      }
    });
  });

  /* ---------------------------------------------------------------- countdown */
  IP.register("countdown", function (el) {
    var until = Date.parse(el.getAttribute("data-expires") || "");
    if (isNaN(until)) return;
    var reload = el.getAttribute("data-reload") === "1";
    var timer;
    function pad(n) {
      return (n < 10 ? "0" : "") + n;
    }
    function tick() {
      var left = Math.round((until - Date.now()) / 1000);
      if (left <= 0) {
        clearInterval(timer);
        el.textContent = "expired";
        el.classList.add("text-bad");
        // pages decide how to refresh (the desk waits while the owner is typing)
        if (reload) IP.emit("countdown.done", { el: el });
        return;
      }
      el.textContent = Math.floor(left / 60) + ":" + pad(left % 60);
      if (left <= 60) el.classList.add("text-bad");
    }
    tick();
    timer = setInterval(tick, 1000);
  });

  /* ---------------------------------------------------------------- owner desk: live refresh */
  IP.register("desk-live", function (root) {
    var pending = null;
    var banner = null;

    function busy() {
      var a = document.activeElement;
      if (a && /^(INPUT|SELECT|TEXTAREA)$/.test(a.tagName) && root.contains(a)) return true;
      var dirty = false;
      root.querySelectorAll("form[data-component~='manual-sale'] input:not([type=hidden]), form[data-component~='manual-sale'] textarea").forEach(function (f) {
        if (f.value) dirty = true;
      });
      return dirty;
    }

    function showBanner() {
      if (banner) return;
      banner = IP.h(
        "div",
        { class: "alert alert-info mb-6 animate-fade-up", role: "status" },
        IP.h("p", { class: "flex-1", text: "New activity at the desk." }),
        IP.h("button", { type: "button", class: "btn btn-dark btn-sm", text: "Refresh", onclick: function () { location.reload(); } })
      );
      root.insertBefore(banner, root.firstChild);
    }

    function refresh() {
      if (busy()) showBanner();
      else location.reload();
    }

    IP.on("notification", function (evt) {
      if (!evt || typeof evt.link !== "string" || evt.link.indexOf("/owner/desk") !== 0) return;
      clearTimeout(pending);
      pending = setTimeout(refresh, 700);
    });
    IP.on("countdown.done", function () {
      clearTimeout(pending);
      pending = setTimeout(refresh, 1500);
    });
    // fallback when the live connection is down: refresh every minute
    setInterval(function () {
      if (!(IP.ws && IP.ws.connected) && !busy() && document.visibilityState === "visible") location.reload();
    }, 60000);
  });

  /* ---------------------------------------------------------------- Cashfree JS SDK checkout */
  IP.register("cashfree-checkout", function (el) {
    var sessionId = el.getAttribute("data-session-id");
    var mode = el.getAttribute("data-mode") === "production" ? "production" : "sandbox";
    var status = el.querySelector("[data-cashfree-status]");
    var button = el.querySelector("[data-cashfree-launch]");
    var opened = false;

    function say(text) {
      if (status) status.textContent = text;
    }

    function launch() {
      if (!sessionId) {
        say("This payment session is missing. Go back and press Pay again.");
        return;
      }
      if (typeof window.Cashfree !== "function") {
        say("The secure payment window couldn't load. Check your connection and try again.");
        return;
      }
      try {
        var cashfree = window.Cashfree({ mode: mode });
        opened = true;
        say("Redirecting to Cashfree…");
        cashfree.checkout({ paymentSessionId: sessionId, redirectTarget: "_self" });
      } catch (err) {
        opened = false;
        say("The payment window couldn't open (" + (err && err.message ? err.message : "unknown error") + ").");
      }
    }

    if (button) button.addEventListener("click", launch);
    var tries = 0;
    (function wait() {
      if (opened) return;
      if (typeof window.Cashfree === "function") return launch();
      if (tries++ > 40) return say("The secure payment window couldn't load. Press “Open payment window” to retry.");
      setTimeout(wait, 250);
    })();
  });

  /* ---------------------------------------------------------------- owner: plan editor cards */
  IP.register("plan-editor", function (form) {
    var save = form.querySelector("[data-save-button]");
    var initial = new FormData(form);
    function snapshot(fd) {
      return ["price", "description", "is_enabled"].map(function (k) {
        return k + "=" + (fd.get(k) || "");
      }).join("&");
    }
    var start = snapshot(initial);
    function check() {
      var dirty = snapshot(new FormData(form)) !== start;
      form.toggleAttribute("data-dirty", dirty);
      if (save) {
        save.textContent = dirty ? "Save changes" : "Save";
        save.classList.toggle("btn-primary", dirty);
        save.classList.toggle("btn-dark", !dirty);
      }
    }
    form.addEventListener("input", check);
    form.addEventListener("change", check);
  });

  /* ---------------------------------------------------------------- owner desk: cash registration form */
  IP.register("manual-sale", function (form) {
    var planSelect = form.querySelector("[data-plan-select]");
    var branchField = form.querySelector("[data-branch-field]");
    var branchSelect = form.querySelector("[data-branch-select]");
    if (!planSelect || !branchField || !branchSelect) return;

    function sync() {
      var opt = planSelect.options[planSelect.selectedIndex];
      var gym = opt ? opt.getAttribute("data-gym") : null;
      var needs = !!opt && opt.getAttribute("data-needs-branch") === "1";
      branchField.hidden = !needs;
      branchSelect.required = needs;
      var visible = 0;
      Array.prototype.forEach.call(branchSelect.options, function (o) {
        if (!o.value) return;
        var show = !gym || o.getAttribute("data-gym") === gym;
        o.hidden = !show;
        o.disabled = !show;
        if (show) visible++;
        if (!show && o.selected) branchSelect.value = "";
      });
      if (needs && visible === 0) toast("No open branch", "This gym has no open branch to register the customer at.", "warning");
    }

    planSelect.addEventListener("change", sync);
    sync();
  });
})();
