/* Dynamic Island: idle pill (AI status, clock, unread count) -> expands for live notifications ->
 * opens a notification centre panel. Public API:
 *   IP.island.notify({title, body, tone: "info|success|warning|danger", link, timeout})
 *   IP.island.activity(key, {label, tone})   // sticky "live activity" (e.g. "Desk OTP pending")
 *   IP.island.clearActivity(key)
 * Server contract (WP-A2): GET /api/notifications?limit=20[&before_id=] -> {items:[{id,title,body,severity,link,created_at,read}],
 *                            unread, has_more, next_before_id}   ("Show older" appends the next keyset window)
 *                          GET /api/notifications/unread -> {unread, capped}   (page load: one bounded count, no feed)
 *                          POST /api/notifications/read-all, POST /api/notifications/{id}/read
 * The badge count is cached per tab (sessionStorage) for UNREAD_TTL ms after the server last confirmed it, so moving
 * between pages does not ask again; live "notification" events and local reads adjust it in between.
 */
(function () {
  "use strict";
  var IP = window.IP;
  var TONES = { info: "#0a84ff", success: "#30d158", warning: "#ff9f0a", danger: "#ff453a" };
  var UNREAD_KEY = "ip.island.unread";
  var UNREAD_TTL = 30000;
  var ICONS = {
    info: '<circle cx="12" cy="12" r="10"/><line x1="12" y1="16" x2="12" y2="12"/><line x1="12" y1="8" x2="12.01" y2="8"/>',
    success: '<polyline points="20 6 9 17 4 12"/>',
    warning: '<path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/>',
    danger: '<circle cx="12" cy="12" r="10"/><line x1="15" y1="9" x2="9" y2="15"/><line x1="9" y1="9" x2="15" y2="15"/>',
  };

  IP.register("island", function (el) {
    var layers = {};
    el.querySelectorAll("[data-layer]").forEach(function (l) {
      layers[l.getAttribute("data-layer")] = l;
    });
    var titleEl = el.querySelector("[data-title]");
    var bodyEl = el.querySelector("[data-body]");
    var iconEl = el.querySelector("[data-icon]");
    var unreadEl = el.querySelector("[data-unread]");
    var clockEl = el.querySelector("[data-clock]");
    var listEl = el.querySelector("[data-list]");
    var authed = el.getAttribute("data-auth") === "1";
    var queue = [];
    var busy = false;
    var unread = 0;
    var unreadAt = 0; // when the server last confirmed `unread` (0 = never)
    var userId = (document.body && document.body.getAttribute("data-user-id")) || "";
    var activities = {};
    var state = "idle";

    function show(name) {
      state = name;
      el.setAttribute("data-state", name === "activity" ? "activity" : name);
      Object.keys(layers).forEach(function (k) {
        var visible = k === name || (name === "activity" && k === "expanded");
        if (visible) layers[k].removeAttribute("hidden");
        else layers[k].setAttribute("hidden", "");
      });
    }

    function showUnread(n) {
      unread = Math.max(0, n | 0);
      unreadEl.textContent = unread > 99 ? "99+" : String(unread);
      if (unread > 0) unreadEl.removeAttribute("hidden");
      else unreadEl.setAttribute("hidden", "");
      document.querySelectorAll("[data-unread-dot]").forEach(function (d) {
        d.classList.toggle("hidden", unread === 0);
      });
      if (unreadAt && userId) {
        try {
          window.sessionStorage.setItem(UNREAD_KEY, JSON.stringify({ u: userId, n: unread, t: unreadAt }));
        } catch (e) {
          /* storage disabled: every page asks the server */
        }
      }
    }

    // a count the server just confirmed (island fetches, 11-account.js after marking read)
    function setUnread(n) {
      unreadAt = Date.now();
      showUnread(n);
    }

    // a local change (live event, a click that marks one read): keeps the age of the last server answer
    function bumpUnread(delta) {
      showUnread(unread + delta);
    }

    function cachedUnread() {
      try {
        var c = JSON.parse(window.sessionStorage.getItem(UNREAD_KEY) || "null");
        var age = c ? Date.now() - c.t : -1;
        if (c && c.u === userId && typeof c.n === "number" && age >= 0 && age < UNREAD_TTL) return c;
      } catch (e) {
        /* unreadable: ask the server */
      }
      return null;
    }

    function paint(n) {
      var tone = TONES[n.tone] ? n.tone : "info";
      titleEl.textContent = n.title || "";
      bodyEl.textContent = n.body || "";
      iconEl.style.background = TONES[tone];
      iconEl.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="size-5">' + ICONS[tone] + "</svg>";
      el.setAttribute("data-link", n.link || "");
    }

    function next() {
      if (busy || state === "panel") return;
      var n = queue.shift();
      if (!n) {
        var keys = Object.keys(activities);
        if (keys.length) {
          var a = activities[keys[keys.length - 1]];
          paint({ title: a.label, body: a.detail || "", tone: a.tone });
          show("activity");
        } else show("idle");
        return;
      }
      busy = true;
      paint(n);
      show("expanded");
      setTimeout(function () {
        busy = false;
        next();
      }, n.timeout || 4200);
    }

    IP.island = {
      notify: function (n) {
        queue.push(n || {});
        next();
      },
      activity: function (key, a) {
        activities[key] = a || {};
        if (!busy && state !== "panel") next();
      },
      clearActivity: function (key) {
        delete activities[key];
        if (!busy && state !== "panel") next();
      },
      setUnread: setUnread,
      open: openPanel,
    };

    // one window of the feed; `append` adds an older window below the current one ("Show older")
    function renderList(data, append) {
      var items = (data && data.items) || [];
      var more = listEl.querySelector("[data-island-more]");
      if (more) more.remove();
      if (!append) listEl.textContent = "";
      if (!append && !items.length) {
        listEl.appendChild(IP.h("p", { class: "px-2 py-6 text-center text-sm text-white/50", text: "You're all caught up." }));
        return;
      }
      items.forEach(function (it) {
        var row = IP.h(
          it.link ? "a" : "div",
          { class: "island-item", href: it.link || null, "data-unread": it.read ? "0" : "1" },
          IP.h("span", { class: "mt-1 inline-block size-2 shrink-0 rounded-full", style: null }),
          IP.h(
            "span",
            { class: "min-w-0 flex-1" },
            IP.h("span", { class: "block truncate text-[13px] font-semibold", text: it.title }),
            IP.h("span", { class: "block text-xs text-white/60", text: it.body }),
            IP.h("span", { class: "mt-0.5 block text-[11px] text-white/40", text: IP.timeAgo(it.created_at) })
          )
        );
        row.firstChild.style.background = TONES[{ success: "success", warning: "warning", danger: "danger" }[it.severity] || "info"];
        if (!it.read) {
          row.addEventListener("click", function () {
            bumpUnread(-1);
            IP.api("/api/notifications/" + it.id + "/read", { method: "POST" }).catch(function () {});
          });
        }
        listEl.appendChild(row);
      });
      if (data.has_more && data.next_before_id) {
        var btn = IP.h("button", {
          type: "button",
          class: "mx-auto mt-2 block rounded-full bg-white/10 px-3 py-1.5 text-xs font-semibold hover:bg-white/20",
          "data-island-more": "1",
          text: "Show older",
        });
        btn.addEventListener("click", function (e) {
          e.stopPropagation();
          if (btn.disabled) return;
          btn.disabled = true;
          btn.textContent = "Loading…";
          IP.api("/api/notifications?limit=20&before_id=" + encodeURIComponent(data.next_before_id))
            .then(function (older) {
              renderList(older, true);
            })
            .catch(function () {
              btn.disabled = false;
              btn.textContent = "Show older";
            });
        });
        listEl.appendChild(btn);
      }
    }

    function loadList() {
      if (!authed) return;
      IP.api("/api/notifications?limit=20")
        .then(function (data) {
          renderList(data);
          setUnread(data.unread || 0);
        })
        .catch(function () {
          listEl.textContent = "";
          listEl.appendChild(IP.h("p", { class: "px-2 py-6 text-center text-sm text-white/50", text: "Couldn't load notifications." }));
        });
    }

    function openPanel() {
      queue = [];
      busy = false;
      show("panel");
      loadList();
    }

    el.addEventListener("click", function (e) {
      if (e.target.closest("[data-island-close]")) {
        e.stopPropagation();
        show("idle");
        next();
        return;
      }
      if (e.target.closest("[data-read-all]")) {
        e.stopPropagation();
        IP.api("/api/notifications/read-all", { method: "POST" }).then(loadList).catch(function () {});
        return;
      }
      if (state === "expanded" && el.getAttribute("data-link")) {
        location.href = el.getAttribute("data-link");
        return;
      }
      if (state !== "panel") openPanel();
    });
    document.querySelectorAll("[data-island-open]").forEach(function (b) {
      b.addEventListener("click", function (e) {
        e.stopPropagation();
        openPanel();
      });
    });
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape" && state === "panel") {
        show("idle");
        next();
      }
    });
    document.addEventListener("click", function (e) {
      if (state === "panel" && !el.contains(e.target)) {
        show("idle");
        next();
      }
    });

    // clock
    function tick() {
      clockEl.textContent = new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    }
    tick();
    setInterval(tick, 15000);

    // AI status
    function setAi(stateName, label) {
      document.querySelectorAll("[data-ai-dot]").forEach(function (d) {
        d.setAttribute("data-state", stateName);
      });
      var t = el.querySelector("[data-island-ai]");
      if (t) t.textContent = stateName === "offline" ? "AI off" : "AI";
      var txt = el.querySelector("[data-ai-text]");
      if (txt) txt.textContent = stateName === "offline" ? "AI unavailable - videos still work" : label || "AI online";
      document.documentElement.setAttribute("data-ai", stateName);
    }
    IP.on("hello", function (evt) {
      if (evt.ai) setAi(evt.ai.state, evt.ai.active);
    });
    IP.on("ai.status", function (evt) {
      setAi(evt.state, evt.active);
      IP.island.notify({
        title: evt.available ? "AI is back online" : "AI is unavailable",
        body: evt.available ? "Now using " + (evt.active || "AI") + "." : "All AI providers are offline. Pre-recorded videos still work.",
        tone: evt.available ? "success" : "warning",
      });
    });
    IP.on("notification", function (evt) {
      bumpUnread(1);
      IP.island.notify({ title: evt.title, body: evt.body, tone: evt.severity, link: evt.link });
      if (state === "panel") loadList();
    });

    if (authed) {
      var cached = cachedUnread();
      if (cached) {
        unreadAt = cached.t;
        showUnread(cached.n);
      } else {
        IP.api("/api/notifications/unread")
          .then(function (d) {
            setUnread(d.unread || 0);
          })
          .catch(function () {});
      }
    }
    var aiState = el.getAttribute("data-ai-state");
    if (aiState === "offline") IP.island.activity("ai", { label: "AI unavailable", detail: "Videos and non-AI features still work", tone: "warning" });
    IP.on("ai.status", function (evt) {
      if (evt.available) IP.island.clearActivity("ai");
      else IP.island.activity("ai", { label: "AI unavailable", detail: "Videos still work", tone: "warning" });
    });
    show("idle");
    next();
  });
})();
