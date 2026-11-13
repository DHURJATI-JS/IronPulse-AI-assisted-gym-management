/* 00-core.js */
/* IronPulse core runtime: namespace, tab identity, CSRF-aware fetch, component registry, event bus, WebSocket.
 * No inline scripts/handlers anywhere (strict CSP): pages declare behaviour with data-component="name".
 * Bundled (in filename order) + minified into /static/dist/app.min.js by scripts/build_assets.py.
 *
 * Tab identity (docs/ai-guard-and-queue.md section 7): IP.pageId is a random id for this document. It travels as the
 * X-Page-Id header of every IP.api / IP.ai request, as a hidden page_id field of POST forms and as /ws?page=<id>, so
 * the server can stop the AI work of a page as soon as it is closed (the socket closes with code 4001 on pagehide)
 * without touching the user's other tabs. Hiding a tab (visibilitychange) never cancels anything: live workouts
 * lock the phone.
 */
(function () {
  "use strict";
  var IP = (window.IP = window.IP || {});
  var components = {};
  var listeners = {};
  var MAX_RETRY_AFTER = 7 * 86400; // seconds; also keeps every derived setTimeout below the 2^31 ms overflow
  var WS_QUEUE_MAX = 20; // messages kept while disconnected (the server closes with 4429 above 40 per 10 s)
  var WS_BUSY_BASE_MS = 2000; // 1013 "try again later" (the server is overloaded): ~2 s for the first try ...
  var WS_BUSY_MAX_MS = 20000; // ... doubling up to 20 s - each spread over 0.5x .. 1.5x (at most 30 s, well inside
  // the server's presence grace period of 45 s, so a page turned away is never taken for a user who left)

  /* ---------------------------------------------------------------- tab identity */
  /** 128 random bits for this document. crypto.randomUUID only exists in secure contexts (https, localhost), so a
   * phone on the gym Wi-Fi opening http://<lan-ip>:port gets hex from crypto.getRandomValues instead. Kept in
   * memory only: sessionStorage is copied into duplicated tabs and survives reloads, which would merge two pages. */
  function newPageId() {
    var c = window.crypto;
    try {
      if (c && typeof c.randomUUID === "function") return c.randomUUID();
    } catch (e) {
      /* insecure context in an engine that throws instead of hiding the method */
    }
    var bytes = null;
    try {
      if (c && typeof c.getRandomValues === "function") bytes = c.getRandomValues(new Uint8Array(16));
    } catch (e) {
      bytes = null;
    }
    var hex = "";
    for (var i = 0; i < 16; i++) {
      var b = bytes ? bytes[i] : Math.floor(Math.random() * 256); // no Web Crypto at all: still a unique tab id
      hex += (b < 16 ? "0" : "") + b.toString(16);
    }
    return hex;
  }
  IP.pageId = newPageId();

  /* ---------------------------------------------------------------- double-submit protection (app/core/submit_guard.py)
   * Forms rendered with the csrf() macro carry a hidden one-time submit_id; IP.api sends X-Submit-Id (one per call, or
   * opts.submitId for a deliberate retry of the same request). The server processes the first request with an id and
   * replays its outcome to duplicates. */
  var SUBMIT_ID_RE = /^[A-Za-z0-9_-]{16,64}$/;
  IP.newSubmitId = function () {
    return newPageId().replace(/[^A-Za-z0-9]/g, "");
  };

  /** True for URLs on this origin (relative paths included): the page id never leaves the site. */
  function sameOrigin(url) {
    var s = String(url == null ? "" : url);
    try {
      return new URL(s, location.href).origin === location.origin;
    } catch (e) {
      return s.charAt(0) === "/" && s.charAt(1) !== "/";
    }
  }

  IP.csrf = function () {
    var m = document.querySelector('meta[name="csrf-token"]');
    return m ? m.getAttribute("content") : "";
  };

  /** Register a component: IP.register("chart", function (el) { ... }) -> runs for each [data-component~=chart]. */
  IP.register = function (name, init) {
    components[name] = init;
  };

  IP.boot = function (root) {
    (root || document).querySelectorAll("[data-component]").forEach(function (el) {
      var names = (el.getAttribute("data-component") || "").split(/\s+/);
      el.__ipInit = el.__ipInit || {};
      names.forEach(function (name) {
        if (!name || el.__ipInit[name] || !components[name]) return;
        el.__ipInit[name] = true;
        try {
          components[name](el);
        } catch (err) {
          console.error("[IronPulse] component '" + name + "' failed", err);
        }
      });
    });
  };

  /** Seconds to wait, from a Retry-After header (delta-seconds or an HTTP date) or a JSON retry_after value:
   * a whole number > 0, or null when absent, zero or unreadable. */
  IP.parseRetryAfter = function (value) {
    if (value == null || value === "" || typeof value === "boolean") return null;
    var secs;
    if (typeof value === "number") {
      secs = value;
    } else {
      var text = String(value).trim();
      if (/^\d+(\.\d+)?$/.test(text)) {
        secs = parseFloat(text);
      } else {
        var at = Date.parse(text); // HTTP-date form, e.g. "Wed, 21 Oct 2026 07:28:00 GMT"
        secs = isNaN(at) ? NaN : (at - Date.now()) / 1000;
      }
    }
    if (!isFinite(secs) || secs <= 0) return null;
    return Math.min(Math.ceil(secs), MAX_RETRY_AFTER);
  };

  /** The Error IP.api throws for a non-2xx response (shared with IP.ai.stream): message from data.detail or
   * data.error, plus .status, .data, .code (data.code, e.g. "quota") and .retryAfter (seconds or null; the larger
   * of the Retry-After header and data.retry_after, so a client never asks again too early). */
  IP.apiError = function (res, data) {
    var body = data && typeof data === "object" ? data : null;
    var msg = (body && (body.detail || body.error)) || "Request failed (" + res.status + ")";
    var err = new Error(typeof msg === "string" ? msg : "Request failed");
    err.status = res.status;
    err.data = data;
    err.code = body && body.code != null ? body.code : null;
    var header = IP.parseRetryAfter(res.headers && res.headers.get ? res.headers.get("retry-after") : null);
    var field = IP.parseRetryAfter(body ? body.retry_after : null);
    err.retryAfter = header == null ? field : field == null ? header : Math.max(header, field);
    var signedOut = res.headers && res.headers.get ? res.headers.get("x-signed-out") : null;
    if (res.status === 401 && signedOut === "elsewhere") {
      err.code = err.code || "signed_out_elsewhere";
      IP.signedOutElsewhere(err.message);
    }
    return err;
  };

  /** One session per account: this browser was signed out because the account signed in on another device (the
   * socket closed with 4409, or a request answered 401 + X-Signed-Out: elsewhere). Say so, stop reconnecting and go to
   * the sign-in page, which explains which device and when (and offers the password reset). Runs once per page. */
  var signedOutShown = false;
  IP.signedOutElsewhere = function (message) {
    if (signedOutShown) return;
    signedOutShown = true;
    wsStopped = true;
    if (wsTimer) {
      clearTimeout(wsTimer);
      wsTimer = null;
    }
    var text =
      typeof message === "string" && message
        ? message
        : "You were signed out because your account signed in on another device.";
    if (IP.island && typeof IP.island.notify === "function") {
      IP.island.notify({ title: "Signed out on this device", body: text, tone: "warning", timeout: 6000 });
    }
    IP.emit("session.signed_out", { reason: "elsewhere", message: text });
    if (location.pathname.indexOf("/auth/") === 0) return; // already on the sign-in pages
    var next = location.pathname + (location.search || "");
    setTimeout(function () {
      location.assign("/auth/login?elsewhere=1&next=" + encodeURIComponent(next));
    }, 1200);
  };

  /** fetch wrapper: JSON in/out, CSRF header on unsafe methods, X-Page-Id on every same-origin request,
   * opts.keepalive for requests that must outlive the page. Throws IP.apiError(...) (.status, .data, .code,
   * .retryAfter) on failure. */
  IP.api = async function (url, opts) {
    opts = opts || {};
    var method = (opts.method || "GET").toUpperCase();
    var init = { method: method, credentials: "same-origin", headers: { Accept: "application/json" }, signal: opts.signal };
    if (method !== "GET" && method !== "HEAD") {
      init.headers["X-CSRF-Token"] = IP.csrf();
      // one id per call (a component retrying the very same request passes opts.submitId): the server runs it once
      if (sameOrigin(url)) {
        init.headers["X-Submit-Id"] = SUBMIT_ID_RE.test(String(opts.submitId || "")) ? opts.submitId : IP.newSubmitId();
      }
    }
    if (IP.pageId && sameOrigin(url)) init.headers["X-Page-Id"] = IP.pageId;
    if (opts.keepalive) init.keepalive = true;
    if (opts.json !== undefined) {
      init.headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(opts.json);
    } else if (opts.form) {
      init.body = opts.form instanceof FormData ? opts.form : new FormData(opts.form);
    }
    var form = opts.form && opts.form.tagName ? opts.form : null; // a <form> element: its busy buttons come back below
    var res;
    try {
      res = await fetch(url, init);
    } catch (netErr) {
      if (form) IP.formDone(form);
      throw netErr;
    }
    var ct = res.headers.get("content-type") || "";
    var data;
    try {
      if (ct.indexOf("application/json") !== -1) {
        try {
          data = await res.json();
        } catch (parseErr) {
          if (res.ok) throw parseErr;
          data = null; // a broken error body must not hide the status / Retry-After
        }
      } else {
        data = await res.text();
      }
    } finally {
      if (form) IP.formDone(form);
    }
    if (!res.ok) throw IP.apiError(res, data);
    return data;
  };

  IP.escape = function (s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  };

  /** Safe element builder: IP.h("div", {class: "x", text: "hello"}, child...). Never uses innerHTML for data. */
  IP.h = function (tag, attrs) {
    var el = document.createElement(tag);
    attrs = attrs || {};
    Object.keys(attrs).forEach(function (k) {
      var v = attrs[k];
      if (v == null || v === false) return;
      if (k === "text") el.textContent = v;
      else if (k === "class") el.className = v;
      else if (k.indexOf("on") === 0 && typeof v === "function") el.addEventListener(k.slice(2), v);
      else el.setAttribute(k, v === true ? "" : v);
    });
    for (var i = 2; i < arguments.length; i++) {
      var c = arguments[i];
      if (c == null) continue;
      el.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
    }
    return el;
  };

  IP.on = function (type, fn) {
    (listeners[type] = listeners[type] || []).push(fn);
  };
  IP.emit = function (type, detail) {
    (listeners[type] || []).concat(listeners["*"] || []).forEach(function (fn) {
      try {
        fn(detail, type);
      } catch (err) {
        console.error(err);
      }
    });
  };

  IP.timeAgo = function (iso) {
    var d = new Date(iso);
    var s = Math.round((Date.now() - d.getTime()) / 1000);
    if (isNaN(s)) return "";
    if (s < 45) return "just now";
    var units = [["y", 31536000], ["mo", 2592000], ["w", 604800], ["d", 86400], ["h", 3600], ["m", 60]];
    for (var i = 0; i < units.length; i++) if (s >= units[i][1]) return Math.floor(s / units[i][1]) + units[i][0] + " ago";
    return "just now";
  };

  IP.fmtTime = function (iso) {
    try {
      return new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    } catch (e) {
      return "";
    }
  };

  /* ---------------------------------------------------------------- WebSocket (live chat, notifications, AI status)
   * Close codes: 4001 = this page is hidden/closing (sent by us on pagehide: the server stops the page's AI work at
   * once, we never reconnect), 4401 no session / 4403 bad origin or banned (no reconnect), 4409 signed out because
   * the account signed in on another device (no reconnect: a message, then the sign-in page), 4429 flooding (we back
   * off for at least 30 s), 1013 try again later (the server is overloaded: a jittered back-off - 1-3 s, 2-6 s, 4-12 s
   * ... at most 10-30 s - so the pages it turned away do not all come back in the same second; a socket that gets its
   * "hello" starts over). Anything else reconnects with exponential back-off (1 s ... 30 s). */
  IP.ws = {
    socket: null,
    connected: false,
    retries: 0,
    floods: 0,
    busy: 0, // 1013 closes in a row
    queue: [],
    send: function (obj) {
      var s = this.socket;
      if (s && s.readyState === 1) s.send(JSON.stringify(obj));
      else {
        this.queue.push(obj);
        if (this.queue.length > WS_QUEUE_MAX) this.queue.splice(0, this.queue.length - WS_QUEUE_MAX);
      }
    },
  };
  var wsTimer = null; // pending reconnect
  var wsStopped = false; // between pagehide and a bfcache restore: never (re)connect

  function connectWs() {
    wsTimer = null;
    if (wsStopped || !document.body || document.body.getAttribute("data-ws") !== "on" || !("WebSocket" in window)) return;
    var current = IP.ws.socket;
    if (current && (current.readyState === 0 || current.readyState === 1)) return; // already connecting / open
    var proto = location.protocol === "https:" ? "wss" : "ws";
    var sock;
    try {
      sock = new WebSocket(proto + "://" + location.host + "/ws?page=" + encodeURIComponent(IP.pageId));
    } catch (e) {
      return;
    }
    IP.ws.socket = sock;
    var ping;
    var openedAt = 0;
    sock.onopen = function () {
      if (IP.ws.socket !== sock) {
        // replaced while connecting (bfcache restore): that page id is gone, never keep a second socket
        sock.__ipIntentional = true;
        try {
          sock.close(4001, "page-hide");
        } catch (e) {
          /* already closing */
        }
        return;
      }
      openedAt = Date.now();
      IP.ws.connected = true;
      IP.ws.retries = 0;
      IP.emit("ws.open", {});
      while (IP.ws.queue.length) sock.send(JSON.stringify(IP.ws.queue.shift()));
      ping = setInterval(function () {
        if (sock.readyState === 1) sock.send('{"type":"ping"}');
      }, 25000);
    };
    sock.onmessage = function (e) {
      var evt;
      try {
        evt = JSON.parse(e.data);
      } catch (err) {
        return;
      }
      if (evt && evt.type === "hello") IP.ws.busy = 0; // the server took us in: a later 1013 starts over
      if (evt && evt.type) IP.emit(evt.type, evt);
    };
    sock.onclose = function (e) {
      clearInterval(ping);
      if (IP.ws.socket !== sock) return; // an old socket closing late (e.g. after a bfcache restore replaced it)
      var intentional = !!sock.__ipIntentional;
      IP.ws.connected = false;
      IP.emit("ws.close", { code: e.code, intentional: intentional });
      if (intentional || wsStopped) return; // we closed it on purpose (page hidden / closing): no reconnect
      if (e.code === 4409) {
        IP.signedOutElsewhere(); // one session per account: the newest sign-in won
        return;
      }
      if (e.code === 4401 || e.code === 4403) return; // signed out / forbidden: don't hammer
      var delay;
      if (e.code === 4429) {
        // flood guard: wait at least 30 s (doubling on repeats, max 5 min); a long-lived socket starts over
        IP.ws.floods = openedAt && Date.now() - openedAt > 300000 ? 1 : IP.ws.floods + 1;
        delay = Math.min(300000, 30000 * Math.pow(2, IP.ws.floods - 1)) + Math.random() * 5000;
      } else if (e.code === 1013) {
        // try again later: the server is overloaded (its database). Back off by a window that doubles while it lasts
        // (2 s ... 20 s), spread at random over 0.5x .. 1.5x of it - never in lockstep with the other pages turned away
        IP.ws.busy += 1;
        delay = Math.min(WS_BUSY_MAX_MS, WS_BUSY_BASE_MS * Math.pow(2, IP.ws.busy - 1)) * (0.5 + Math.random());
      } else {
        if (openedAt && Date.now() - openedAt > 60000) IP.ws.floods = 0;
        delay = Math.min(30000, 1000 * Math.pow(2, IP.ws.retries++)) + Math.random() * 500;
      }
      wsTimer = setTimeout(connectWs, delay);
    };
  }

  /** Close our socket on purpose (code 4001): the server cancels this page's AI work right away. */
  function closeWsForPageHide() {
    var s = IP.ws.socket;
    if (s && (s.readyState === 0 || s.readyState === 1)) {
      s.__ipIntentional = true;
      try {
        s.close(4001, "page-hide");
      } catch (e) {
        /* already closing */
      }
    }
    IP.ws.connected = false;
  }

  /* ---------------------------------------------------------------- page lifecycle */
  // pagehide: the document is being unloaded or frozen into the back/forward cache -> stop the socket for good.
  window.addEventListener("pagehide", function () {
    wsStopped = true;
    if (wsTimer) {
      clearTimeout(wsTimer);
      wsTimer = null;
    }
    closeWsForPageHide();
  });
  // pageshow with persisted: restored from the back/forward cache. The server may already have cancelled the old
  // page's work, so this is a new page: new id, fresh socket.
  window.addEventListener("pageshow", function (e) {
    if (!e || !e.persisted) return;
    var previous = IP.pageId;
    IP.pageId = newPageId();
    wsStopped = false;
    IP.ws.retries = 0;
    if (wsTimer) {
      clearTimeout(wsTimer);
      wsTimer = null;
    }
    closeWsForPageHide();
    IP.ws.socket = null;
    connectWs();
    // forms submitted before the page was frozen get their buttons back (the same submit id stays: an unchanged
    // resubmission is answered with the first outcome, a changed one is a new request - app/core/submit_guard.py)
    document.querySelectorAll("form[data-ip-busy]").forEach(function (form) {
      IP.formDone(form);
    });
    IP.emit("page.restored", { pageId: IP.pageId, previous: previous });
  });

  /* ---------------------------------------------------------------- busy submit buttons
   * On submit the form's submit buttons are disabled (the clicked one gets aria-busy and a short label: its
   * data-busy-label, else "Please wait..."), so a double click sends one request. They come back on a back/forward
   * cache return, when an IP.api call with this form ends (an error answer shows its message, the user may correct
   * and resend) and after BUSY_MS at the latest (a stalled connection). Forms with data-no-busy manage themselves. */
  var BUSY_MS = 15000;
  var BUSY_LABEL = "Please wait…";

  function submitButtons(form) {
    var out = [];
    form.querySelectorAll('button[type="submit"], button:not([type]), input[type="submit"]').forEach(function (b) {
      out.push(b);
    });
    return out;
  }

  function setBusy(form, submitter) {
    if (!form || form.hasAttribute("data-no-busy") || form.hasAttribute("data-ip-busy")) return;
    var buttons = submitButtons(form).filter(function (b) {
      return !b.disabled;
    });
    if (!buttons.length) return;
    form.setAttribute("data-ip-busy", "");
    var main = submitter && buttons.indexOf(submitter) !== -1 ? submitter : buttons[0];
    buttons.forEach(function (b) {
      b.disabled = true;
      b.setAttribute("data-ip-busy", "");
    });
    main.setAttribute("aria-busy", "true");
    var label = main.getAttribute("data-busy-label") || BUSY_LABEL;
    if (String(main.tagName).toUpperCase() === "INPUT") {
      main.__ipLabel = main.value;
      main.value = label;
    } else if ((main.textContent || "").trim()) {
      main.__ipKids = Array.prototype.slice.call(main.childNodes); // the button's own nodes, put back unchanged
      main.textContent = label;
    }
    form.__ipBusyTimer = setTimeout(function () {
      IP.formDone(form);
    }, BUSY_MS);
  }

  /** Give a submitted form its buttons back (components call it when they handle the submission themselves). */
  IP.formDone = function (form) {
    if (!form || !form.hasAttribute || !form.hasAttribute("data-ip-busy")) return;
    form.removeAttribute("data-ip-busy");
    if (form.__ipBusyTimer) {
      clearTimeout(form.__ipBusyTimer);
      form.__ipBusyTimer = null;
    }
    submitButtons(form).forEach(function (b) {
      if (!b.hasAttribute("data-ip-busy")) return;
      b.removeAttribute("data-ip-busy");
      b.removeAttribute("aria-busy");
      b.disabled = false;
      if (b.__ipKids) {
        b.textContent = "";
        b.__ipKids.forEach(function (n) {
          b.appendChild(n);
        });
        b.__ipKids = null;
      }
      if (b.__ipLabel !== undefined) {
        b.value = b.__ipLabel;
        b.__ipLabel = undefined;
      }
    });
  };

  /** True for a submission that POSTs to this site (the submitter's formmethod / formaction win). */
  function samePost(form, submitter) {
    if (!form || String(form.tagName || "").toUpperCase() !== "FORM") return false;
    var sub = submitter && submitter.getAttribute ? submitter : null;
    var method = ((sub && sub.getAttribute("formmethod")) || form.getAttribute("method") || "get").toLowerCase();
    if (method !== "post") return false;
    var action = (sub && sub.getAttribute("formaction")) || form.getAttribute("action") || "";
    return !action || sameOrigin(action); // never hand our ids to another site
  }

  /** Plain HTML forms cannot send headers: give every same-origin POST form a hidden page_id field (the same id
   * as X-Page-Id) so routes that start AI work can tie it to this tab. Runs in the capture phase, before any
   * component's own submit handler serialises the form. */
  function stampPageId(form, submitter) {
    if (!form || String(form.tagName || "").toUpperCase() !== "FORM" || !IP.pageId) return;
    var sub = submitter && submitter.getAttribute ? submitter : null;
    var method = ((sub && sub.getAttribute("formmethod")) || form.getAttribute("method") || "get").toLowerCase();
    if (method !== "post") return;
    var action = (sub && sub.getAttribute("formaction")) || form.getAttribute("action") || "";
    if (action && !sameOrigin(action)) return; // never hand our id to another site
    var input = form.querySelector('input[name="page_id"]');
    if (!input) {
      input = document.createElement("input");
      input.setAttribute("type", "hidden");
      input.setAttribute("name", "page_id");
      input.setAttribute("data-page-id", "");
      form.appendChild(input);
    }
    if (input.hasAttribute("data-page-id") || !input.value) input.value = IP.pageId; // ours, or an empty slot
  }

  /* ---------------------------------------------------------------- generic behaviours */
  function wireGlobal() {
    // tab identity for plain HTML forms (capture phase: before component handlers read the form)
    document.addEventListener(
      "submit",
      function (e) {
        stampPageId(e.target, e.submitter);
      },
      true
    );
    // the submit id the csrf() macro rendered: a submission a component handled itself (preventDefault + fetch) leaves
    // this page as it was, so the next one is a new request with a fresh id. A real navigation keeps it - a double
    // click stays one request (app/core/submit_guard.py).
    document.addEventListener(
      "submit",
      function (e) {
        var form = e.target;
        if (!samePost(form, e.submitter)) return;
        var input = form.querySelector('input[name="submit_id"]');
        if (!input) return;
        setTimeout(function () {
          if (e.defaultPrevented) input.value = IP.newSubmitId();
        }, 0);
      },
      true
    );
    // confirmation for destructive forms: <form data-confirm="Are you sure?">
    document.addEventListener("submit", function (e) {
      var form = e.target;
      var msg = form.getAttribute && form.getAttribute("data-confirm");
      if (msg && !window.confirm(msg)) {
        e.preventDefault();
        return;
      }
      // prevent double submits: after this event (the submitter's name/value is already in the form data)
      var submitter = e.submitter || null;
      setTimeout(function () {
        setBusy(form, submitter);
      }, 0);
    });
    // auto-submit selects/checkboxes: <select data-autosubmit>
    document.addEventListener("change", function (e) {
      if (e.target.hasAttribute && e.target.hasAttribute("data-autosubmit") && e.target.form) e.target.form.requestSubmit();
    });
    // dismiss buttons inside alerts
    document.addEventListener("click", function (e) {
      var d = e.target.closest && e.target.closest("[data-dismiss]");
      if (d) {
        var box = d.closest(".alert") || d.parentElement;
        if (box) box.remove();
      }
      var copy = e.target.closest && e.target.closest("[data-copy]");
      if (copy && navigator.clipboard) {
        navigator.clipboard.writeText(copy.getAttribute("data-copy"));
        if (IP.island) IP.island.notify({ title: "Copied", body: copy.getAttribute("data-copy"), tone: "success", timeout: 1800 });
      }
    });
    // mobile navigation
    var toggle = document.querySelector("[data-nav-toggle]");
    var menu = document.querySelector("[data-mobile-nav]");
    if (toggle && menu) {
      toggle.addEventListener("click", function () {
        var open = menu.classList.toggle("hidden") === false;
        toggle.setAttribute("aria-expanded", open ? "true" : "false");
      });
    }
    // close <details data-component="dropdown"> when clicking outside
    document.addEventListener("click", function (e) {
      document.querySelectorAll('details[data-component~="dropdown"][open]').forEach(function (d) {
        if (!d.contains(e.target)) d.removeAttribute("open");
      });
    });
  }

  document.addEventListener("DOMContentLoaded", function () {
    wireGlobal();
    IP.boot(document);
    connectWs();
  });
})();

;
/* 01-ai.js */
/* IP.ai - the one client for AI requests (docs/ai-guard-and-queue.md section 7). AI widgets go through it so that
 * per-user limits, the fair AI queue and "stop when you leave" behave the same everywhere.
 *
 *   IP.ai.request(url, {method: "POST", json, form, signal, feature, timeout, keepalive, safety}) -> Promise<body>
 *   IP.ai.stream(url, {json, signal, feature, onEvent, timeout, safety}) -> Promise<{done: last "done" event or null,
 *                                                                           blocked: the "blocked" event or null}>
 *   IP.ai.describeError(err)     -> one friendly sentence for any error from request / stream / IP.api
 *   IP.ai.cooldownLeft(feature)  -> whole seconds before `feature` may ask the server again (0 = now)
 *   IP.ai.noteRefusal(feature, code, seconds) -> records a refusal that arrived inside a 200 answer (the live coach's
 *                                   `refused`) exactly like a 429 with that code and retry_after; true when a wait
 *                                   was recorded. `feature` may be left out for code "cooldown" only; code
 *                                   "duplicate" records nothing (it refused some words, never the feature).
 *   IP.ai.onCooldown(fn)         -> fn({feature, seconds, code}) when a cooldown starts and
 *                                   fn({feature, seconds: 0, code, ended: true}) when it ends; returns unsubscribe
 *   IP.ai.abortAll(reason)       -> aborts every in-flight AI request of this page, returns how many
 *   IP.ai.pending()              -> number of in-flight AI requests
 *   IP.ai.formatWait(seconds)    -> "45 s" | "2 min" | "3 h" (for countdown labels)
 *
 * `feature` is the server's policy name (assistant, plans, shop, live_coach, tts, intake ...); without it the URL
 * path is the cooldown bucket. Errors look like IP.api's (.status, .code, .retryAfter, .data). An active cooldown
 * refuses locally - {status: 429, code: "cooldown_local", retryAfter, cooldownCode} - without any network request.
 * A refusal (429/409/422/503 with retry_after > 0, or one handed to noteRefusal) starts one for its feature; code
 * "cooldown" (AI paused for the account) pauses every feature. Code "duplicate" (409 / SSE "blocked") pauses nothing:
 * the server refused those words only, so only the same words - normalised like its check: NFKC, lower case, spaces,
 * edge punctuation - in the same body, for the same feature and URL, are refused locally for the rest of its window
 * ({status: 409, code: "duplicate", retryAfter, local: true}, no request); any other message is sent at once. A 429
 * is never retried automatically: every refused request costs the user strike points and the gym's shared IP its
 * rate-limit budget. Aborts reject with
 * {name: "AbortError", code: "cancelled" | "timeout", reason: "page-hide" | "timeout" | "caller" | <abortAll reason>}.
 *
 * Events: emits "ai.cooldown" {feature (null: the whole account), seconds, code} and "ai.cooldown.end"; listens to
 * the WebSocket's "ai.queue" events of this page (evt.page_id === IP.pageId) and to SSE "queued" events for the
 * "Waiting for AI" island activity, and to "task.update" for subtle background-task notifications. pagehide aborts
 * every in-flight request (keepalive ones excepted). Never anything on visibilitychange: live workouts lock the phone.
 * `safety: true` marks a safety flow (discomfort reports, health red flags): never refused by a local cooldown and
 * never aborted by abortAll / pagehide - the server never blocks those either (policy.safety).
 * Bundled after 00-core.js and before 05-island.js, so IP.island is looked up lazily (it exists once booted).
 */
(function () {
  "use strict";
  var IP = window.IP;
  if (!IP || IP.ai) return; // core missing, or the bundle was included twice

  var GLOBAL = "*"; // cooldown bucket of code "cooldown": AI is paused for the whole account
  var REFUSALS = { 409: true, 422: true, 429: true, 503: true };
  var MAX_TIMER_MS = 2147483647;
  var QUEUE_TTL_MS = 60000; // a WebSocket queue entry without news for this long is stale (its end event was lost)
  var TASK_NAMES = {
    coach_plan: "Your training plan",
    health_ai_note: "Your health note",
    health_ac_note: "Your health note",
    payroll_explain: "Payroll explanation",
    verify_identity: "ID check",
    verify_branch: "Branch verification",
    verify_payment_proof: "Payment proof check",
    staff_activation_check: "Staff activation check",
  };
  var CODE_TEXT = {
    // code -> [what happened, what to do when the server gave no wait time]
    quota: ["You've reached your AI limit for now.", "Try again later."],
    cooldown: ["AI is paused for your account after too many requests.", "Try again later."],
    too_many: ["You already have AI requests running - wait for them to finish.", ""],
    duplicate: ["You just asked that.", "Wait for the answer or ask something different."],
    spam: ["That message looks like spam, so it wasn't sent. Please rephrase it.", ""],
    busy: ["The AI is very busy right now.", "Please try again in a moment."],
    disabled: ["AI isn't available for your account.", ""],
  };

  var inflight = []; // {id, key, dup, ctrl, reason, keep, undo[]} - one per running request / stream
  var seq = 0;
  var cooldowns = {}; // bucket -> {until: epoch ms, code}
  var cooldownTimers = {};
  var cooldownFns = [];
  var repeats = {}; // feature + URL path + normalised words -> until (epoch ms): words just refused as a duplicate
  var MAX_REPEATS = 64;
  // whitespace as Python's str.split() sees it (the server's normalize_text), and punctuation trimmed at the edges
  var SPACE = "\\t\\n\\v\\f\\r \\x1c-\\x1f\\x85\\xa0\\u1680\\u2000-\\u200a\\u2028\\u2029\\u202f\\u205f\\u3000";
  var SPACES = new RegExp("[" + SPACE + "]+", "g");
  var EDGES = edgeNoise();
  var queued = {}; // queue-UI entries: key -> {position, expires}
  var queueShown = false;
  var queueSweep = null;
  var tasks = {}; // task_id -> last status this page saw

  function island() {
    var isl = IP.island;
    return isl && typeof isl.notify === "function" ? isl : null;
  }

  function logError(err) {
    if (window.console && console.error) console.error(err);
  }

  /* ---------------------------------------------------------------- cooldowns */
  /** A feature name stays as is; a URL becomes its path (no query), so "/api/x?a=1" and "/api/x" share a bucket. */
  function normKey(value) {
    var s = String(value == null ? "" : value);
    if (s.charAt(0) !== "/" && s.indexOf("://") === -1) return s;
    try {
      return new URL(s, location.href).pathname;
    } catch (e) {
      return s.split("#")[0].split("?")[0];
    }
  }

  function keyFor(feature, url) {
    return normKey(typeof feature === "string" && feature ? feature : url);
  }

  function remainingMs(key) {
    var c = cooldowns[key];
    return c ? Math.max(0, c.until - Date.now()) : 0;
  }

  function cooldownLeft(feature) {
    var ms = remainingMs(GLOBAL);
    if (feature != null && feature !== "") ms = Math.max(ms, remainingMs(normKey(feature)));
    return ms > 0 ? Math.ceil(ms / 1000) : 0;
  }

  function notifyCooldown(info) {
    cooldownFns.slice().forEach(function (fn) {
      try {
        fn(info);
      } catch (err) {
        logError(err);
      }
    });
  }

  function onCooldown(fn) {
    if (typeof fn !== "function") return function () {};
    cooldownFns.push(fn);
    return function () {
      var i = cooldownFns.indexOf(fn);
      if (i !== -1) cooldownFns.splice(i, 1);
    };
  }

  function endCooldown(key) {
    delete cooldownTimers[key];
    var c = cooldowns[key];
    if (!c) return;
    var left = c.until - Date.now();
    if (left > 0) {
      cooldownTimers[key] = setTimeout(function () {
        endCooldown(key);
      }, Math.min(left + 50, MAX_TIMER_MS));
      return;
    }
    delete cooldowns[key];
    var info = { feature: key === GLOBAL ? null : key, seconds: 0, code: c.code, ended: true };
    IP.emit("ai.cooldown.end", info);
    notifyCooldown(info);
  }

  function startCooldown(key, seconds, code) {
    var until = Date.now() + seconds * 1000;
    var cur = cooldowns[key];
    if (cur && cur.until >= until) return; // never shorten a wait the server asked for
    cooldowns[key] = { until: until, code: code || null };
    clearTimeout(cooldownTimers[key]);
    cooldownTimers[key] = setTimeout(function () {
      endCooldown(key);
    }, Math.min(seconds * 1000 + 50, MAX_TIMER_MS));
  }

  /* ---------------------------------------------------------------- duplicates: those words only, never the feature */
  function edgeNoise() {
    try {
      return new RegExp("^[" + SPACE + "\\p{P}]+|[" + SPACE + "\\p{P}]+$", "gu");
    } catch (e) {
      // no Unicode property escapes: ASCII punctuation only (trimming less than the server is safe, more is not)
      return new RegExp("^[" + SPACE + "!-#%-*,-/:;?@\\[-\\]_{}]+|[" + SPACE + "!-#%-*,-/:;?@\\[-\\]_{}]+$", "g");
    }
  }

  /** Words as the server's duplicate check compares them (app/ai/guard.py normalize_text): NFKC, lower case, runs of
   * whitespace as one space, punctuation trimmed at both ends - a text that is only punctuation keeps it. */
  function normWords(value) {
    var s = String(value);
    if (typeof s.normalize === "function") {
      try {
        s = s.normalize("NFKC");
      } catch (e) {
        /* keep it as typed */
      }
    }
    s = s.toLowerCase().replace(SPACES, " ").trim();
    return s.replace(EDGES, "") || s;
  }

  /** The words a request sends (its JSON body, or its form's text fields), normalised; "" when it sends none. */
  function wordsOf(opts) {
    var parts = [];
    var body = opts.json;
    if (body !== undefined) {
      if (body === null || typeof body !== "object") {
        parts.push("=" + normWords(body));
      } else {
        Object.keys(body).sort().forEach(function (k) {
          var v = body[k];
          if (v == null) return;
          parts.push(k + (typeof v === "object" ? ":" + JSON.stringify(v) : "=" + normWords(v)));
        });
      }
    } else if (opts.form && typeof FormData === "function") {
      try {
        var fd = opts.form instanceof FormData ? opts.form : new FormData(opts.form);
        if (typeof fd.forEach !== "function") return "";
        fd.forEach(function (v, k) {
          // csrf_token, page_id and the one-time submit_id (double-submit guard) change with every render of the form
          if (typeof v === "string" && k !== "csrf_token" && k !== "page_id" && k !== "submit_id") parts.push(k + "=" + normWords(v));
        });
      } catch (e) {
        return "";
      }
      parts.sort();
    }
    return parts.join("\u0001");
  }

  /** What a duplicate refusal blocks: these words, for this feature, at this URL - path and query, which may name the
   * subject - ("" = nothing to block). */
  function repeatKey(key, url, opts) {
    var words = wordsOf(opts);
    if (!words) return "";
    var where = String(url == null ? "" : url).split("#")[0];
    try {
      var u = new URL(where, location.href);
      where = u.pathname + u.search;
    } catch (e) {
      /* keep it as given */
    }
    return key + "\n" + where + "\n" + words;
  }

  function blockRepeat(rkey, seconds) {
    if (!rkey) return;
    var now = Date.now();
    Object.keys(repeats).forEach(function (k) {
      if (repeats[k] <= now) delete repeats[k];
    });
    if (Object.keys(repeats).length >= MAX_REPEATS) repeats = {}; // never grows without bound; the server still judges
    repeats[rkey] = Math.max(repeats[rkey] || 0, now + seconds * 1000);
  }

  /** The error the same words produce while their duplicate window runs - the server's 409, without the request. */
  function repeatRefusal(key, rkey) {
    var until = rkey ? repeats[rkey] : 0;
    if (!until) return null;
    var ms = until - Date.now();
    if (ms <= 0) {
      delete repeats[rkey];
      return null;
    }
    var err = new Error(CODE_TEXT.duplicate[0]);
    err.status = 409;
    err.code = "duplicate";
    err.retryAfter = Math.ceil(ms / 1000);
    err.feature = key;
    err.local = true;
    err.message = describeError(err);
    err.data = { error: "Already asked", detail: err.message, code: "duplicate", retry_after: err.retryAfter };
    return err;
  }

  /** The server refused with a wait: do not ask again before it would answer. A duplicate refuses only its own words
   * (`rkey`), for the rest of the server's window - never the whole feature. */
  function noteRefusal(key, status, code, seconds, rkey) {
    if (!REFUSALS[status] || !(seconds > 0)) return;
    code = typeof code === "string" && code ? code : null;
    if (code === "duplicate") {
      blockRepeat(rkey, seconds);
      return;
    }
    startCooldown(key, seconds, code);
    if (code === "cooldown") startCooldown(GLOBAL, seconds, code);
    var info = { feature: key === GLOBAL ? null : key, seconds: seconds, code: code };
    IP.emit("ai.cooldown", info);
    notifyCooldown(info);
  }

  /** IP.ai.noteRefusal: a refusal inside a 200 answer has the same effect as a 429 with that code and retry_after - a
   * cooldown for `feature` (a policy name, or a URL as in request), every feature when the code is "cooldown", and the
   * "ai.cooldown" event. `seconds` is read like Retry-After. False (nothing recorded) without a wait, or without a
   * feature unless the whole account is paused, and for code "duplicate" (a refusal of those words only, which
   * pauses nothing). */
  function noteServerRefusal(feature, code, seconds) {
    if (code === "duplicate") return false;
    var key = feature == null || feature === "" ? "" : normKey(feature);
    var secs = IP.parseRetryAfter(seconds);
    if (!secs || (!key && code !== "cooldown")) return false;
    noteRefusal(key || GLOBAL, 429, code, secs);
    return true;
  }

  /** The error an active cooldown produces instead of a network request. */
  function localRefusal(key) {
    var seconds = cooldownLeft(key);
    if (!seconds) return null;
    var c = remainingMs(GLOBAL) >= remainingMs(key) ? cooldowns[GLOBAL] : cooldowns[key];
    var err = new Error("Please wait before asking again.");
    err.status = 429;
    err.code = "cooldown_local";
    err.retryAfter = seconds;
    err.cooldownCode = (c && c.code) || null;
    err.feature = key;
    err.local = true;
    err.message = describeError(err);
    err.data = { error: "Please wait", detail: err.message, code: "cooldown_local", retry_after: seconds };
    return err;
  }

  /* ---------------------------------------------------------------- in-flight requests */
  function newController() {
    if (typeof AbortController === "function") return new AbortController();
    return { signal: undefined, abort: function () {} }; // very old browsers: the result is dropped instead
  }

  function abortEntry(entry, reason) {
    if (entry.reason) return; // the first reason wins
    entry.reason = reason || "aborted";
    try {
      entry.ctrl.abort();
    } catch (e) {
      /* nothing left to abort */
    }
  }

  function track(key, opts, rkey) {
    var keep = !!(opts.keepalive || opts.safety);
    var entry = { id: ++seq, key: key, dup: rkey || "", ctrl: newController(), reason: null, keep: keep, undo: [] };
    inflight.push(entry);
    var signal = opts.signal;
    if (signal && typeof signal.addEventListener === "function") {
      if (signal.aborted) {
        abortEntry(entry, "caller");
      } else {
        var onAbort = function () {
          abortEntry(entry, "caller");
        };
        signal.addEventListener("abort", onAbort);
        entry.undo.push(function () {
          signal.removeEventListener("abort", onAbort);
        });
      }
    }
    var ms = Number(opts.timeout) || 0;
    if (ms > 0) {
      var timer = setTimeout(function () {
        abortEntry(entry, "timeout");
      }, Math.min(ms, MAX_TIMER_MS));
      entry.undo.push(function () {
        clearTimeout(timer);
      });
    }
    return entry;
  }

  function untrack(entry) {
    var i = inflight.indexOf(entry);
    if (i !== -1) inflight.splice(i, 1);
    var undo = entry.undo;
    entry.undo = [];
    undo.forEach(function (fn) {
      fn();
    });
    dropQueued("s" + entry.id);
  }

  function abortError(entry) {
    var reason = entry.reason || "aborted";
    var text = "The AI request was cancelled.";
    if (reason === "timeout") text = "The AI took too long to answer.";
    else if (reason === "page-hide") text = "Stopped because the page was closed.";
    var err = new Error(text);
    err.name = "AbortError";
    err.code = reason === "timeout" ? "timeout" : "cancelled";
    err.reason = reason;
    err.feature = entry.key;
    return err;
  }

  /** Normalise a failure: our aborts become one AbortError shape; refusals with a wait start a cooldown. */
  function failure(entry, err) {
    if (entry.reason || (err && err.name === "AbortError")) {
      if (!entry.reason) entry.reason = "aborted";
      return abortError(entry);
    }
    if (err && typeof err === "object") {
      err.feature = entry.key;
      if (err.status) noteRefusal(entry.key, err.status, err.code, err.retryAfter, entry.dup);
    }
    return err;
  }

  function abortAll(reason) {
    var n = 0;
    inflight.slice().forEach(function (entry) {
      if (entry.keep || entry.reason) return; // keepalive requests outlive the page; safety flows are never cut
      abortEntry(entry, reason || "aborted");
      n++;
    });
    return n;
  }

  /* ---------------------------------------------------------------- JSON requests */
  function request(url, opts) {
    opts = opts || {};
    var key = keyFor(opts.feature, url);
    var refused = opts.safety ? null : localRefusal(key);
    if (refused) return Promise.reject(refused);
    var rkey = opts.safety ? "" : repeatKey(key, url, opts);
    var repeated = repeatRefusal(key, rkey);
    if (repeated) return Promise.reject(repeated);
    var entry = track(key, opts, rkey);
    if (entry.reason) {
      untrack(entry);
      return Promise.reject(abortError(entry));
    }
    return IP.api(url, {
      method: opts.method || "POST",
      json: opts.json,
      form: opts.form,
      signal: entry.ctrl.signal,
      keepalive: !!opts.keepalive,
    }).then(
      function (data) {
        untrack(entry);
        if (entry.reason) throw abortError(entry); // aborted while the body was arriving: the caller moved on
        return data;
      },
      function (err) {
        untrack(entry);
        throw failure(entry, err);
      }
    );
  }

  /* ---------------------------------------------------------------- server-sent events over fetch */
  function parseJson(text) {
    try {
      return JSON.parse(text);
    } catch (e) {
      return undefined;
    }
  }

  /** One SSE event block: its data: lines are one JSON object (or, leniently, one object per line). */
  function parseEvent(block, emit) {
    var lines = block.split("\n");
    var data = [];
    var name = "";
    for (var i = 0; i < lines.length; i++) {
      var line = lines[i];
      var colon = line.indexOf(":");
      if (colon === 0) continue; // comment / keep-alive
      var field = colon === -1 ? line : line.slice(0, colon);
      var value = colon === -1 ? "" : line.slice(colon + 1);
      if (value.charAt(0) === " ") value = value.slice(1);
      if (field === "data") data.push(value);
      else if (field === "event") name = value;
    }
    if (!data.length) return;
    var dispatch = function (evt) {
      if (!evt || typeof evt !== "object") return;
      if (name && evt.type == null) evt.type = name;
      emit(evt);
    };
    var whole = parseJson(data.join("\n"));
    if (whole === undefined && data.length > 1) data.forEach(function (d) { dispatch(parseJson(d)); });
    else dispatch(whole);
  }

  /** Hands every complete event in `buf` to `emit`; returns the unfinished tail (all of it is parsed when final). */
  function parseSse(buf, emit, final) {
    var tail = "";
    if (!final && buf.charAt(buf.length - 1) === "\r") {
      tail = "\r"; // half of a CRLF split across two chunks
      buf = buf.slice(0, -1);
    }
    var blocks = buf.replace(/\r\n?/g, "\n").split("\n\n");
    var rest = final ? "" : blocks.pop();
    for (var i = 0; i < blocks.length; i++) parseEvent(blocks[i], emit);
    return rest + tail;
  }

  async function stream(url, opts) {
    opts = opts || {};
    var key = keyFor(opts.feature, url);
    var refused = opts.safety ? null : localRefusal(key);
    if (refused) throw refused;
    var rkey = opts.safety ? "" : repeatKey(key, url, opts);
    var repeated = repeatRefusal(key, rkey);
    if (repeated) throw repeated;
    var entry = track(key, opts, rkey);
    var qkey = "s" + entry.id;
    var result = { done: null, blocked: null };
    var onEvent = typeof opts.onEvent === "function" ? opts.onEvent : null;

    function handle(evt) {
      if (evt.type === "queued") showQueued(qkey, evt.position, evt.eta, false);
      else dropQueued(qkey); // anything else means the request left the queue
      if (evt.type === "blocked") {
        result.blocked = evt;
        noteRefusal(key, 429, evt.code, IP.parseRetryAfter(evt.retry_after), rkey);
      } else if (evt.type === "done") {
        result.done = evt;
      }
      if (onEvent) {
        try {
          onEvent(evt);
        } catch (err) {
          logError(err);
        }
      }
    }

    try {
      if (entry.reason) throw abortError(entry);
      var headers = { "Content-Type": "application/json", Accept: "text/event-stream", "X-CSRF-Token": IP.csrf() };
      if (IP.pageId) headers["X-Page-Id"] = IP.pageId;
      var res = await fetch(url, {
        method: "POST",
        credentials: "same-origin",
        headers: headers,
        body: JSON.stringify(opts.json === undefined ? {} : opts.json),
        signal: entry.ctrl.signal,
      });
      var ct = (res.headers.get("content-type") || "").toLowerCase();
      if (!res.ok || ct.indexOf("application/json") !== -1) {
        var data = null;
        try {
          data = ct.indexOf("application/json") !== -1 ? await res.json() : await res.text();
        } catch (e) {
          data = null;
        }
        var bad = IP.apiError(res, data);
        if (res.ok) {
          bad.code = bad.code || "bad_stream"; // a JSON reply where a stream was expected
          bad.message = "The AI answer could not be read.";
        }
        throw bad;
      }
      var buf = "";
      if (res.body && typeof res.body.getReader === "function") {
        var reader = res.body.getReader();
        var decoder = new TextDecoder();
        for (;;) {
          var step = await reader.read();
          if (step.done) break;
          buf = parseSse(buf + decoder.decode(step.value, { stream: true }), handle, false);
        }
        buf += decoder.decode();
      } else {
        buf = await res.text(); // no streaming bodies: parse it all at the end
      }
      parseSse(buf, handle, true);
      if (entry.reason) throw abortError(entry);
      return result;
    } catch (err) {
      throw failure(entry, err);
    } finally {
      untrack(entry);
    }
  }

  /* ---------------------------------------------------------------- "Waiting for AI" (island live activity) */
  function paintQueue() {
    var now = Date.now();
    var best = null;
    var soonest = Infinity;
    Object.keys(queued).forEach(function (k) {
      var q = queued[k];
      if (q.expires <= now) {
        delete queued[k];
        return;
      }
      soonest = Math.min(soonest, q.expires);
      if (!best || q.position < best.position) best = q;
    });
    if (queueSweep) clearTimeout(queueSweep);
    queueSweep = soonest < Infinity ? setTimeout(paintQueue, Math.min(soonest - now + 50, MAX_TIMER_MS)) : null;
    var isl = island();
    if (!isl) return;
    if (best) {
      queueShown = true;
      isl.activity("ai-queue", {
        label: "Waiting for AI",
        detail: best.position === 1 ? "You are next" : best.position - 1 + " ahead",
        tone: "info",
      });
    } else if (queueShown) {
      queueShown = false;
      isl.clearActivity("ai-queue");
    }
  }

  /** `ttl`: WebSocket entries expire when no news arrives; a stream's entry lives until the stream moves on. */
  function showQueued(key, position, eta, ttl) {
    var pos = Math.max(1, Math.floor(Number(position)) || 1);
    var secs = Number(eta) > 0 ? Number(eta) : 0;
    queued[key] = { position: pos, expires: ttl ? Date.now() + Math.max(QUEUE_TTL_MS, secs * 2000 + 15000) : Infinity };
    paintQueue();
  }

  function dropQueued(key) {
    if (!queued[key]) return;
    delete queued[key];
    paintQueue();
  }

  IP.on("ai.queue", function (evt) {
    if (!evt || evt.page_id !== IP.pageId) return; // the hub sends this to every tab of the user
    var key = "w" + (evt.ticket != null ? evt.ticket : evt.feature);
    if (evt.state === "queued") showQueued(key, evt.position, evt.eta, true);
    else dropQueued(key); // running | done | cancelled | expired
  });

  /* ---------------------------------------------------------------- background tasks (subtle island notes) */
  function localLink(link) {
    if (typeof link !== "string" || link.charAt(0) !== "/") return null;
    return link.charAt(1) === "/" || link.charAt(1) === "\\" ? null : link; // protocol-relative: another site
  }

  IP.on("task.update", function (evt) {
    if (!evt || evt.task_id == null || !evt.status) return;
    if (evt.page_id && evt.page_id !== IP.pageId) return;
    var id = String(evt.task_id);
    var status = String(evt.status);
    var prev = tasks[id];
    tasks[id] = status;
    if (status === prev || evt.notify === false) return; // repeats, or a widget shows this task itself
    var isl = island();
    if (!isl) return;
    var name = TASK_NAMES[evt.kind] || "Background task";
    var link = localLink(evt.link);
    if (status === "done") {
      isl.notify({ title: evt.title || name + " - done", body: evt.message || (link ? "Tap to open." : ""), tone: "success",
                   link: link });
    } else if (status === "failed") {
      isl.notify({ title: evt.title || name + " - failed", body: evt.message || "It couldn't be finished. Please try again.",
                   tone: "danger", link: link });
    } else if ((status === "queued" || status === "running") && (prev === "paused" || evt.resumed === true)) {
      isl.notify({ title: name, body: "Paused while you were away - resuming now", tone: "info" });
    }
  });

  /* ---------------------------------------------------------------- friendly errors */
  function formatWait(seconds) {
    var s = Math.ceil(Number(seconds) || 0);
    if (s <= 0) return "";
    if (s < 60) return s + " s";
    if (s < 90 * 60) return Math.ceil(s / 60) + " min";
    return Math.ceil(s / 3600) + " h";
  }

  function withWait(text, err, otherwise) {
    var wait = formatWait(err.retryAfter);
    if (wait) return text + " Try again in " + wait + ".";
    return otherwise ? text + " " + otherwise : text;
  }

  function serverDetail(err) {
    var d = err.data && typeof err.data === "object" ? err.data.detail : null;
    return typeof d === "string" && d.length <= 300 ? d : "";
  }

  function describeError(err) {
    if (err == null) return "Something went wrong - please try again.";
    if (typeof err === "string") return err;
    var code = typeof err.code === "string" ? err.code : "";
    var status = Number(err.status) || 0;
    if (err.name === "AbortError" || code === "cancelled" || code === "timeout") {
      if (code === "timeout" || err.reason === "timeout") return "That took too long - please try again.";
      if (err.reason === "page-hide") return "Stopped because you left the page.";
      return "The request was cancelled.";
    }
    if (code === "cooldown_local") {
      var why = CODE_TEXT[err.cooldownCode];
      return why ? withWait(why[0], err, why[1]) : withWait("Please wait a moment before asking again.", err, "");
    }
    if (CODE_TEXT[code]) return withWait(CODE_TEXT[code][0], err, CODE_TEXT[code][1]);
    if (!status) {
      if (window.navigator && navigator.onLine === false) return "You're offline - check your connection and try again.";
      return "Couldn't reach the server - check your connection and try again.";
    }
    if (status === 401) return "Your session has expired - please sign in again.";
    if (status === 403) return "That request was blocked - reload the page and try again.";
    if (status === 404) return serverDetail(err) || "That couldn't be found - it may have been removed.";
    if (status === 413) return "That's too large to send.";
    if (status === 422) return serverDetail(err) || "Please check what you typed and try again.";
    if (status === 429) return withWait("You're going a bit fast.", err, "Please wait a moment and try again.");
    if (status === 503) return withWait("AI is unavailable right now.", err, "Please try again in a moment.");
    if (status >= 500) return "Something went wrong on our side - please try again.";
    return serverDetail(err) || "Something went wrong - please try again.";
  }

  /* ---------------------------------------------------------------- page lifecycle */
  window.addEventListener("pagehide", function () {
    abortAll("page-hide"); // the socket closes with 4001 too (00-core.js), so the server drops queued work at once
    queued = {};
    paintQueue();
  });
  window.addEventListener("pageshow", function (e) {
    if (!e || !e.persisted) return;
    queued = {}; // restored from the back/forward cache: nothing of the old page is still waiting
    paintQueue();
  });

  IP.ai = {
    request: request,
    stream: stream,
    describeError: describeError,
    cooldownLeft: cooldownLeft,
    noteRefusal: noteServerRefusal,
    onCooldown: onCooldown,
    abortAll: abortAll,
    pending: function () {
      return inflight.length;
    },
    formatWait: formatWait,
  };
})();

;
/* 02-accounts.js */
/* Remembered accounts of the sign-in page - the account chooser ("like Google"); app/services/remembered_accounts.py.
 *
 * localStorage "ip.accounts" holds ONLY {"device": <random key of this browser>, "refs": [{"r": <opaque reference>,
 * "t": <last used, ms>}]} - at most 5 references, never a name, e-mail, photo, password or database id. After a
 * sign-in with "Remember this account on this device" the server hands the new reference over in a short-lived cookie
 * (ip_rmb / __Host-ip_rmb), which this file moves into localStorage on the next page and deletes.
 *
 * On the sign-in page ([data-component=account-chooser]) the references are sent to POST /auth/remembered, which
 * answers with each account's CURRENT name, masked e-mail and photo (a short-lived signed URL) - or "gone" (unknown,
 * revoked, banned or deleted): those entries are removed from this device at once. Choosing an account fills the form's
 * hidden account_ref (the password is always asked); "Remove" deletes the entry here AND revokes it on the server;
 * "Use another account" shows the plain form. Everything is built with IP.h / textContent - no HTML from data, no
 * inline code (strict CSP). Without JavaScript the plain form works as before.
 */
(function () {
  "use strict";
  var IP = window.IP;
  var KEY = "ip.accounts";
  var MAX = 5;
  var REF_RE = /^[A-Za-z0-9_-]{40,64}$/;
  var DEVICE_RE = /^[A-Za-z0-9_-]{22,64}$/;
  var PHOTO_PREFIX = "/auth/remembered/photo/";
  var HANDOFF = ["__Host-ip_rmb", "ip_rmb"];

  function storage() {
    try {
      return window.localStorage || null;
    } catch (e) {
      return null; // blocked (privacy settings): nothing is remembered on this device
    }
  }

  function read() {
    var store = storage();
    var data = null;
    try {
      data = JSON.parse((store && store.getItem(KEY)) || "null");
    } catch (e) {
      data = null;
    }
    if (!data || typeof data !== "object") data = {};
    var seen = {};
    var refs = (Array.isArray(data.refs) ? data.refs : []).filter(function (x) {
      if (!x || typeof x.r !== "string" || !REF_RE.test(x.r) || seen[x.r]) return false;
      seen[x.r] = true;
      return true;
    });
    return {
      device: typeof data.device === "string" && DEVICE_RE.test(data.device) ? data.device : "",
      refs: refs.slice(0, MAX).map(function (x) {
        return { r: x.r, t: typeof x.t === "number" && isFinite(x.t) ? x.t : 0 };
      }),
    };
  }

  function write(state) {
    var store = storage();
    if (!store) return false;
    try {
      store.setItem(KEY, JSON.stringify({ device: state.device, refs: state.refs.slice(0, MAX) }));
      return true;
    } catch (e) {
      return false;
    }
  }

  /** This browser's random key (no fingerprinting): the server keeps one reference per account per device and at
   * most 5 per device. */
  function deviceKey(state) {
    if (!state.device) {
      state.device = IP.newSubmitId();
      write(state);
    }
    return state.device;
  }

  function remember(ref) {
    var state = read();
    state.refs = state.refs.filter(function (x) {
      return x.r !== ref;
    });
    state.refs.unshift({ r: ref, t: Date.now() });
    state.refs = state.refs.slice(0, MAX); // the server applies the same cap to this device
    deviceKey(state);
    write(state);
  }

  function forgetLocal(refs) {
    var state = read();
    var before = state.refs.length;
    state.refs = state.refs.filter(function (x) {
      return refs.indexOf(x.r) === -1;
    });
    if (state.refs.length !== before) write(state);
  }

  /** The reference the server set after a sign-in with "remember": into localStorage, cookie deleted. */
  function takeHandoff() {
    var cookies = document.cookie ? document.cookie.split(/;\s*/) : [];
    cookies.forEach(function (pair) {
      var i = pair.indexOf("=");
      if (i < 1) return;
      var name = pair.slice(0, i);
      if (HANDOFF.indexOf(name) === -1) return;
      var value = pair.slice(i + 1);
      if (REF_RE.test(value)) remember(value);
      var secure = name.indexOf("__Host-") === 0 || location.protocol === "https:" ? "; Secure" : "";
      document.cookie = name + "=; Max-Age=0; Path=/; SameSite=Strict" + secure;
    });
  }

  IP.accounts = { read: read, remember: remember, forget: forgetLocal, takeHandoff: takeHandoff, MAX: MAX };

  function avatar(acc, size) {
    var box = IP.h("span", { class: "avatar " + size, "aria-hidden": "true" });
    var photo = typeof acc.photo === "string" && acc.photo.indexOf(PHOTO_PREFIX) === 0 ? acc.photo : "";
    if (photo) {
      var img = IP.h("img", { src: photo, alt: "", loading: "lazy", decoding: "async" });
      img.addEventListener("error", function () {
        box.textContent = acc.initials || "?";
      });
      box.appendChild(img);
    } else {
      box.textContent = String(acc.initials || "?").slice(0, 3);
    }
    return box;
  }

  IP.register("account-chooser", function (root) {
    var form = document.querySelector("form[data-login-form]");
    if (!form) return;
    var state = read();
    var deviceInput = form.querySelector("[data-device-id]");
    if (deviceInput && storage()) deviceInput.value = deviceKey(state);
    var refInput = form.querySelector("[data-account-ref]");
    var idField = form.querySelector("[data-identifier-field]");
    var idInput = idField ? idField.querySelector("input") : null;
    var chosen = form.querySelector("[data-chosen-account]");
    var list = root.querySelector("[data-chooser-list]");
    var other = root.querySelector("[data-chooser-other]");
    var lookupUrl = root.getAttribute("data-lookup") || "/auth/remembered";
    var forgetUrl = root.getAttribute("data-forget") || "/auth/remembered/forget";
    var accounts = [];
    if (!refInput || !idField || !idInput || !chosen || !list) return;

    function showIdentifier(focus) {
      refInput.value = "";
      chosen.hidden = true;
      idField.hidden = false;
      idInput.disabled = false;
      idInput.required = true;
      if (focus) idInput.focus();
    }

    function choose(acc) {
      refInput.value = acc.ref;
      var slot = chosen.querySelector("[data-chosen-avatar]");
      if (slot) {
        slot.textContent = "";
        slot.appendChild(avatar(acc, "size-10 text-sm"));
      }
      var name = chosen.querySelector("[data-chosen-name]");
      var email = chosen.querySelector("[data-chosen-email]");
      if (name) name.textContent = acc.name || "";
      if (email) email.textContent = acc.email || "";
      idInput.value = "";
      idInput.required = false;
      idInput.disabled = true; // not sent: the server signs in with the chosen reference - the password is still asked
      idField.hidden = true;
      chosen.hidden = false;
      var pw = form.querySelector('input[type="password"]');
      if (pw) pw.focus();
    }

    function forget(acc) {
      forgetLocal([acc.ref]);
      accounts = accounts.filter(function (a) {
        return a.ref !== acc.ref;
      });
      if (refInput.value === acc.ref) showIdentifier(false);
      render();
      IP.api(forgetUrl, { method: "POST", json: { ref: acc.ref } }).catch(function () {
        /* already gone here; the server forgets unused references by itself */
      });
    }

    function render() {
      list.textContent = "";
      accounts.forEach(function (acc) {
        var pick = IP.h(
          "button",
          {
            type: "button",
            class:
              "flex min-w-0 flex-1 items-center gap-3 rounded-2xl border border-black/[0.08] px-3 py-2.5 text-left " +
              "transition hover:bg-black/[0.03] dark:border-white/10 dark:hover:bg-white/[0.05]",
            "data-chooser-pick": "",
          },
          avatar(acc, "size-10 text-sm"),
          IP.h(
            "span",
            { class: "min-w-0" },
            IP.h("span", { class: "block truncate font-semibold", text: acc.name || "" }),
            acc.email ? IP.h("span", { class: "block truncate text-sm muted", text: acc.email }) : null
          )
        );
        pick.addEventListener("click", function () {
          choose(acc);
        });
        var remove = IP.h("button", {
          type: "button",
          class: "shrink-0 rounded-full px-3 py-2 text-xs font-semibold text-zinc-500 hover:text-bad",
          "aria-label": "Remove " + (acc.name || "this account") + " from this device",
          title: "Remove from this device",
          "data-chooser-remove": "",
          text: "Remove",
        });
        remove.addEventListener("click", function () {
          forget(acc);
        });
        list.appendChild(IP.h("li", { class: "flex items-center gap-1" }, pick, remove));
      });
      root.hidden = accounts.length === 0;
    }

    if (other) {
      other.addEventListener("click", function () {
        showIdentifier(true);
      });
    }
    var switcher = chosen.querySelector("[data-chooser-switch]");
    if (switcher) {
      switcher.addEventListener("click", function () {
        showIdentifier(true);
      });
    }

    var preset = refInput.value; // a failed attempt with a chosen account comes back with it
    if (!state.refs.length) {
      if (preset) showIdentifier(false);
      return;
    }
    IP.api(lookupUrl, {
      method: "POST",
      json: {
        refs: state.refs.map(function (x) {
          return x.r;
        }),
      },
    })
      .then(function (data) {
        var items = data && Array.isArray(data.accounts) ? data.accounts : [];
        var gone = [];
        accounts = [];
        items.forEach(function (a) {
          if (!a || typeof a.ref !== "string") return;
          if (a.state === "gone") gone.push(a.ref);
          else if (a.state === "ok") accounts.push(a);
        });
        if (gone.length) forgetLocal(gone); // banned, deleted, revoked or unknown: off this device
        render();
        var again = preset
          ? accounts.filter(function (a) {
              return a.ref === preset;
            })[0]
          : null;
        if (again) choose(again);
        else if (preset) showIdentifier(false);
      })
      .catch(function () {
        /* offline or rate-limited: keep every entry (only an explicit "gone" removes one); the plain form works */
        if (preset) showIdentifier(false);
      });
  });

  document.addEventListener("DOMContentLoaded", takeHandoff);
})();

;
/* 05-island.js */
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

;
/* 06-theme.js */
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

;
/* 07-geolocate.js */
/* "Use my location" component.
 *   <button data-component="geolocate" data-save="1" data-reload="1">Use my location</button>
 * - asks the browser for GPS coordinates (permission prompt), plus the device time zone
 * - data-save="1": POST /api/me/location {lat, lon, accuracy, timezone} (WP-A2 endpoint; signed-in users)
 * - fills hidden inputs name="lat" / name="lon" / name="timezone" in the closest <form> (if any)
 * - data-reload="1": reloads the page afterwards (server re-renders distance checks)
 * - emits IP.emit("location", {lat, lon, accuracy, timezone})
 */
(function () {
  "use strict";
  var IP = window.IP;

  IP.deviceTimezone = function () {
    try {
      return Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
    } catch (e) {
      return "UTC";
    }
  };

  IP.register("geolocate", function (btn) {
    btn.addEventListener("click", function (e) {
      e.preventDefault();
      if (!navigator.geolocation) {
        IP.island && IP.island.notify({ title: "Location unavailable", body: "Your browser does not support geolocation.", tone: "warning" });
        return;
      }
      var label = btn.textContent;
      btn.disabled = true;
      btn.textContent = "Locating…";
      navigator.geolocation.getCurrentPosition(
        function (pos) {
          var data = {
            lat: Math.round(pos.coords.latitude * 1e6) / 1e6,
            lon: Math.round(pos.coords.longitude * 1e6) / 1e6,
            accuracy: Math.round(pos.coords.accuracy || 0),
            timezone: IP.deviceTimezone(),
          };
          var form = btn.closest("form");
          if (form) {
            ["lat", "lon", "timezone"].forEach(function (k) {
              var input = form.querySelector('input[name="' + k + '"]');
              if (input) input.value = data[k];
            });
          }
          IP.emit("location", data);
          var done = function () {
            btn.disabled = false;
            btn.textContent = label;
            IP.island && IP.island.notify({ title: "Location updated", body: "Distances are now based on your current position.", tone: "success", timeout: 2200 });
            if (btn.getAttribute("data-reload") === "1") setTimeout(function () { location.reload(); }, 600);
          };
          if (btn.getAttribute("data-save") === "1" && document.body.getAttribute("data-ws") === "on") {
            IP.api("/api/me/location", { method: "POST", json: data }).then(done).catch(function (err) {
              btn.disabled = false;
              btn.textContent = label;
              IP.island && IP.island.notify({ title: "Couldn't save location", body: err.message, tone: "danger" });
            });
          } else done();
        },
        function (err) {
          btn.disabled = false;
          btn.textContent = label;
          IP.island && IP.island.notify({ title: "Location blocked", body: err.message || "Permission denied.", tone: "warning" });
        },
        { enableHighAccuracy: true, timeout: 15000, maximumAge: 60000 }
      );
    });
  });
})();

;
/* 08-ask-ai.js */
/* "Ask AI" box with streamed answers (server-sent events through IP.ai.stream). Markup: partials/ask_ai.html
 *   <div data-component="ask-ai" data-endpoint="/api/plans/ask" data-extra='{"gym_slug":"x"}'> ... </div>
 * Events: {"type":"meta","provider","cached","busy"} | {"type":"chunk","text"} |
 *         {"type":"blocked","code","detail","retry_after"} | {"type":"done","fallback","note","disclaimer","blocked"}
 * A refusal of the AI guard (quota, cooldown, duplicate, spam ...) arrives as a "blocked" event: its detail replaces
 * the answer, and the ask button and suggestions stay disabled while IP.ai.cooldownLeft("plans") > 0, with a
 * countdown under the form (outside the live region, so screen readers are not told every second). Leaving the page
 * aborts the stream (IP.ai, reason "page-hide") and nothing is shown. Text is rendered with textContent only.
 */
(function () {
  "use strict";
  var IP = window.IP;
  if (!IP || !IP.ai) return;
  var FEATURE = "plans"; // the server's AI policy: the plan advisor's quota and cooldown bucket

  IP.register("ask-ai", function (root) {
    var endpoint = root.getAttribute("data-endpoint");
    var extra = {};
    try {
      extra = JSON.parse(root.getAttribute("data-extra") || "{}") || {};
    } catch (e) {
      extra = {};
    }
    var form = root.querySelector("[data-ask-form]");
    var input = root.querySelector("[data-ask-input]");
    var box = root.querySelector("[data-ask-answer]");
    var text = root.querySelector("[data-ask-text]");
    var typing = root.querySelector("[data-ask-typing]");
    var note = root.querySelector("[data-ask-note]");
    if (!endpoint || !form || !input || !box || !text || !typing || !note) return;
    var waitEl = root.querySelector("[data-ask-wait]");
    var button = form.querySelector("button");
    var suggestions = Array.prototype.slice.call(root.querySelectorAll("[data-ask-suggest]"));
    var section = root.closest ? root.closest("section") : null;
    var providerEl = section ? section.querySelector("[data-ask-provider]") : null;
    var busy = false;
    var ticker = null;

    function setNote(msg) {
      note.textContent = msg || "";
      note.hidden = !msg;
    }

    /** The ask button and the suggestions are off while an answer streams and while the AI cooldown runs. */
    function sync() {
      var wait = IP.ai.cooldownLeft(FEATURE);
      var off = busy || wait > 0;
      if (button) button.disabled = off;
      suggestions.forEach(function (b) {
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

    function handle(evt) {
      if (evt.type === "chunk") {
        typing.hidden = true;
        text.textContent += evt.text || "";
      } else if (evt.type === "meta") {
        if (providerEl) {
          providerEl.textContent = evt.provider
            ? evt.provider + (evt.cached ? " (instant answer)" : "")
            : evt.busy
              ? "AI busy - automatic answer"
              : "AI unavailable - automatic answer";
        }
      } else if (evt.type === "blocked") {
        typing.hidden = true;
        text.textContent = evt.detail || IP.ai.describeError({ code: evt.code, retryAfter: evt.retry_after });
      } else if (evt.type === "done" && evt.note) {
        setNote(evt.note);
      }
    }

    async function ask(question) {
      question = (question || "").trim();
      if (busy || question.length < 2) return;
      busy = true;
      box.classList.remove("hidden");
      text.textContent = "";
      setNote("");
      typing.hidden = false;
      sync();
      var result = null;
      try {
        result = await IP.ai.stream(endpoint, {
          json: Object.assign({ question: question }, extra),
          feature: FEATURE,
          timeout: 45000,
          onEvent: handle,
        });
      } catch (err) {
        if (!(err && err.name === "AbortError" && err.reason === "page-hide")) {
          // a refusal, a cooldown, a timeout or a network failure: say what happened (keep any words already shown)
          if (text.textContent) setNote(IP.ai.describeError(err));
          else text.textContent = IP.ai.describeError(err);
        }
      } finally {
        busy = false;
        typing.hidden = true;
      }
      if (result && !result.done && !result.blocked) {
        // the server stopped the answer (this tab or session was closed elsewhere)
        if (text.textContent) setNote("The answer was interrupted - ask again for the full answer.");
        else text.textContent = "The answer was interrupted - please try again.";
      }
      sync();
    }

    form.addEventListener("submit", function (e) {
      e.preventDefault();
      ask(input.value);
    });
    suggestions.forEach(function (b) {
      b.addEventListener("click", function () {
        input.value = b.getAttribute("data-ask-suggest");
        ask(input.value);
      });
    });
    IP.ai.onCooldown(sync); // a cooldown started by another AI widget of this page (code "cooldown" pauses them all)
    sync();
  });
})();

;
/* 10-public.js */
/* WP-A1 public site behaviour (strict CSP: no inline handlers; DOM built with IP.h / textContent).
 *
 *   data-component="ai-live"        live AI status chip (WebSocket events when signed in, polling otherwise)
 *   data-component="geo-form"       GET form + foundation "geolocate" button: submits lat/lon once located
 *   data-component="gallery"        photo grid -> full-screen lightbox (keyboard, swipe, focus handling)
 *   data-component="branch-map"     Leaflet map from data-lat / data-lon (tiles via /maps/tiles proxy)
 *   data-component="distance-sort"  children with data-lat/data-lon get distance chips + nearest-first order
 *   IP.geo.haversine / formatKm / sortByDistance / lastLocation   distance helpers
 */
(function () {
  "use strict";
  var IP = window.IP;
  if (!IP) return;

  /* ---------------------------------------------------------------- geo helpers */
  var EARTH_KM = 6371.0088;
  var LOC_KEY = "ip-last-location";
  var LOC_MAX_AGE = 30 * 60 * 1000;

  function toRad(d) {
    return (d * Math.PI) / 180;
  }
  function num(v) {
    var n = parseFloat(v);
    return isFinite(n) ? n : null;
  }
  function haversine(lat1, lon1, lat2, lon2) {
    var dLat = toRad(lat2 - lat1);
    var dLon = toRad(lon2 - lon1);
    var a = Math.pow(Math.sin(dLat / 2), 2) + Math.cos(toRad(lat1)) * Math.cos(toRad(lat2)) * Math.pow(Math.sin(dLon / 2), 2);
    return 2 * EARTH_KM * Math.asin(Math.min(1, Math.sqrt(a)));
  }
  function formatKm(d) {
    if (d == null || !isFinite(d)) return "";
    if (d < 1) return Math.max(10, Math.round(d * 100) * 10) + " m";
    if (d < 10) return d.toFixed(1) + " km";
    return Math.round(d) + " km";
  }
  /** [{el, lat, lon}] or elements with data-lat/data-lon -> [{el, km}] nearest first (unknown last). */
  function sortByDistance(items, lat, lon) {
    return Array.prototype.map
      .call(items, function (it) {
        var plain = it && it.el !== undefined; // {el, lat, lon} objects or DOM elements
        var el = plain ? it.el : it;
        var la = plain ? num(it.lat) : num(el.getAttribute("data-lat"));
        var lo = plain ? num(it.lon) : num(el.getAttribute("data-lon"));
        return { el: el, km: la == null || lo == null ? null : haversine(lat, lon, la, lo) };
      })
      .sort(function (a, b) {
        if (a.km == null) return b.km == null ? 0 : 1;
        if (b.km == null) return -1;
        return a.km - b.km;
      });
  }
  function rememberLocation(loc) {
    try {
      sessionStorage.setItem(LOC_KEY, JSON.stringify({ lat: loc.lat, lon: loc.lon, t: Date.now() }));
    } catch (e) {}
  }
  function lastLocation() {
    try {
      var v = JSON.parse(sessionStorage.getItem(LOC_KEY) || "null");
      if (v && num(v.lat) != null && num(v.lon) != null && Date.now() - (v.t || 0) < LOC_MAX_AGE) return v;
    } catch (e) {}
    return null;
  }
  IP.geo = { haversine: haversine, formatKm: formatKm, sortByDistance: sortByDistance, lastLocation: lastLocation };
  IP.on("location", rememberLocation);

  /* ---------------------------------------------------------------- tiny SVG icons for JS-built UI */
  var PATHS = { x: "M18 6 6 18M6 6l12 12", left: "M15 18l-6-6 6-6", right: "M9 18l6-6-6-6", pin: "M21 10c0 7-9 13-9 13s-9-6-9-13a9 9 0 0 1 18 0z" };
  function svgIcon(name, cls) {
    var ns = "http://www.w3.org/2000/svg";
    var svg = document.createElementNS(ns, "svg");
    svg.setAttribute("viewBox", "0 0 24 24");
    svg.setAttribute("fill", "none");
    svg.setAttribute("stroke", "currentColor");
    svg.setAttribute("stroke-width", "2");
    svg.setAttribute("stroke-linecap", "round");
    svg.setAttribute("stroke-linejoin", "round");
    svg.setAttribute("class", cls || "size-5");
    svg.setAttribute("aria-hidden", "true");
    var p = document.createElementNS(ns, "path");
    p.setAttribute("d", PATHS[name]);
    svg.appendChild(p);
    return svg;
  }

  /* ---------------------------------------------------------------- live AI status chip */
  IP.register("ai-live", function (el) {
    var text = el.querySelector("[data-ai-live-text]");
    var dot = el.querySelector("[data-ai-dot]");
    var prefix = el.getAttribute("data-online-prefix") || "AI online";
    function paint(available, state, label) {
      if (dot) dot.setAttribute("data-state", state || (available ? "online" : "offline"));
      if (text) text.textContent = available ? prefix + (label ? " · " + label : "") : "AI unavailable · videos & safety rules still work";
    }
    IP.on("hello", function (evt) {
      if (evt && evt.ai) paint(evt.ai.state !== "offline", evt.ai.state, evt.ai.active);
    });
    IP.on("ai.status", function (evt) {
      paint(!!evt.available, evt.state, evt.active);
    });
    if (document.body.getAttribute("data-ws") !== "on") {
      // guests have no WebSocket: refresh from the public status endpoint while the tab is visible
      setInterval(function () {
        if (document.hidden) return;
        IP.api("/api/ai/status")
          .then(function (s) {
            paint(!!s.available, s.state, s.active);
          })
          .catch(function () {});
      }, 60000);
    }
  });

  /* ---------------------------------------------------------------- "Use my location" forms */
  IP.register("geo-form", function (form) {
    var armed = false;
    form.addEventListener(
      "click",
      function (e) {
        if (e.target.closest && e.target.closest('[data-component~="geolocate"]')) armed = true;
      },
      true
    );
    IP.on("location", function (loc) {
      if (!armed) return;
      armed = false;
      ["lat", "lon"].forEach(function (k) {
        var input = form.querySelector('input[name="' + k + '"]');
        if (input) input.value = loc[k];
      });
      var sent = false;
      function go() {
        if (sent) return;
        sent = true;
        form.submit();
      }
      if (document.body.getAttribute("data-ws") === "on") {
        // signed in: remember it on the account too (never block the page on it)
        setTimeout(go, 2500);
        IP.api("/api/me/location", { method: "POST", json: loc })
          .catch(function () {})
          .then(go);
      } else {
        go();
      }
    });
  });

  /* ---------------------------------------------------------------- gallery lightbox */
  IP.register("gallery", function (root) {
    var links = Array.prototype.slice.call(root.querySelectorAll("[data-gallery-item]"));
    if (!links.length) return;
    var photos = links.map(function (a) {
      return { src: a.getAttribute("href"), caption: a.getAttribute("data-caption") || "" };
    });
    var n = photos.length;
    var box, img, caption, counter, closeBtn, prevBtn, nextBtn;
    var index = 0;
    var lastFocus = null;
    var prevOverflow = "";

    function build() {
      img = IP.h("img", { class: "pub-lightbox-img", alt: "", draggable: "false" });
      caption = IP.h("p", { class: "pub-lightbox-caption" });
      counter = IP.h("p", { class: "shrink-0 text-xs font-semibold tabular-nums text-white/60", "aria-live": "polite" });
      closeBtn = IP.h("button", { type: "button", class: "pub-lightbox-btn", "aria-label": "Close gallery" }, svgIcon("x"));
      prevBtn = IP.h("button", { type: "button", class: "pub-lightbox-btn pub-lightbox-nav left-4", "aria-label": "Previous photo" }, svgIcon("left"));
      nextBtn = IP.h("button", { type: "button", class: "pub-lightbox-btn pub-lightbox-nav right-4", "aria-label": "Next photo" }, svgIcon("right"));
      var stage = IP.h("div", { class: "pub-lightbox-stage" }, prevBtn, img, nextBtn);
      box = IP.h(
        "div",
        { class: "pub-lightbox", role: "dialog", "aria-modal": "true", "aria-label": "Photo gallery", tabindex: "-1", hidden: true },
        IP.h("div", { class: "pub-lightbox-bar" }, counter, closeBtn),
        stage,
        IP.h("div", { class: "pub-lightbox-bar justify-center pb-6" }, caption)
      );
      closeBtn.addEventListener("click", close);
      prevBtn.addEventListener("click", function () {
        show(index - 1);
      });
      nextBtn.addEventListener("click", function () {
        show(index + 1);
      });
      stage.addEventListener("click", function (e) {
        if (e.target === stage) close();
      });
      // swipe left/right to browse, swipe down to close (touch + pen)
      var x0 = null;
      var y0 = null;
      box.addEventListener(
        "touchstart",
        function (e) {
          var t = e.touches[0];
          x0 = t.clientX;
          y0 = t.clientY;
        },
        { passive: true }
      );
      box.addEventListener("touchend", function (e) {
        if (x0 == null) return;
        var t = e.changedTouches[0];
        var dx = t.clientX - x0;
        var dy = t.clientY - y0;
        x0 = y0 = null;
        if (Math.abs(dx) > 45 && Math.abs(dx) > Math.abs(dy) * 1.2) show(index + (dx < 0 ? 1 : -1));
        else if (dy > 90 && Math.abs(dy) > Math.abs(dx)) close();
      });
      // document-level so arrows/Escape keep working after a click moves focus onto the photo
      document.addEventListener("keydown", function (e) {
        if (box.hidden) return;
        if (e.key === "Escape") {
          e.preventDefault();
          close();
        } else if (e.key === "ArrowLeft") {
          e.preventDefault();
          show(index - 1);
        } else if (e.key === "ArrowRight") {
          e.preventDefault();
          show(index + 1);
        } else if (e.key === "Tab") {
          // keep focus inside the dialog
          var f = [closeBtn, prevBtn, nextBtn].filter(function (b) {
            return b.offsetParent !== null;
          });
          var i = f.indexOf(document.activeElement);
          e.preventDefault();
          f[(i + (e.shiftKey ? -1 : 1) + f.length) % f.length].focus();
        }
      });
      if (n < 2) {
        prevBtn.hidden = true;
        nextBtn.hidden = true;
      }
      document.body.appendChild(box);
    }

    function preload(i) {
      var p = photos[(i + n) % n];
      if (p) new Image().src = p.src;
    }

    function show(i) {
      index = (i + n) % n;
      var p = photos[index];
      if (img.getAttribute("src") !== p.src) {
        // fade in once loaded (also on error, so the alt text of a missing photo stays visible)
        img.style.opacity = "0";
        img.onload = img.onerror = function () {
          img.style.opacity = "1";
        };
        img.src = p.src;
      }
      img.alt = p.caption;
      caption.textContent = p.caption;
      counter.textContent = index + 1 + " / " + n;
      preload(index + 1);
      preload(index - 1);
    }

    function open(i) {
      if (!box) build();
      lastFocus = document.activeElement;
      prevOverflow = document.documentElement.style.overflow;
      document.documentElement.style.overflow = "hidden";
      box.hidden = false;
      show(i);
      closeBtn.focus();
    }

    function close() {
      if (!box || box.hidden) return;
      box.hidden = true;
      document.documentElement.style.overflow = prevOverflow;
      if (lastFocus && lastFocus.focus) lastFocus.focus();
    }

    links.forEach(function (a, i) {
      a.addEventListener("click", function (e) {
        if (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey || e.button !== 0) return;
        e.preventDefault();
        open(i);
      });
    });
  });

  /* ---------------------------------------------------------------- branch map (vendored Leaflet) */
  IP.register("branch-map", function (el) {
    var lat = num(el.getAttribute("data-lat"));
    var lon = num(el.getAttribute("data-lon"));
    var fallback = el.querySelector("[data-map-fallback]");
    function fail(msg) {
      var p = fallback && fallback.querySelector("p");
      if (p) p.textContent = msg;
    }
    if (lat == null || lon == null) return fail("This branch hasn't been pinned on the map yet.");
    var L = window.L;
    if (!L || !L.map) return fail("The map couldn't load - use the OpenStreetMap link below.");
    if (fallback) fallback.remove();
    var map = L.map(el, { center: [lat, lon], zoom: 15, scrollWheelZoom: false, zoomControl: true });
    map.attributionControl.setPrefix(false);
    L.tileLayer("/maps/tiles/{z}/{x}/{y}.png", {
      minZoom: 3,
      maxZoom: 19,
      attribution: '&copy; <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener noreferrer">OpenStreetMap</a> contributors',
    }).addTo(map);
    var pin = L.divIcon({ className: "pub-map-pin", html: "<span></span>", iconSize: [26, 26], iconAnchor: [13, 13] });
    L.marker([lat, lon], { icon: pin, keyboard: false, title: el.getAttribute("data-name") || "" }).addTo(map);
    // scroll-zoom only after the visitor engages with the map (page scrolling stays smooth)
    map.once("click focus", function () {
      map.scrollWheelZoom.enable();
    });
    setTimeout(function () {
      map.invalidateSize();
    }, 300);
    var last = lastLocation();
    if (last) {
      var you = L.circleMarker([last.lat, last.lon], { radius: 7, weight: 3, color: "#ffffff", fillColor: "#30d158", fillOpacity: 1 });
      you.addTo(map);
      if (haversine(lat, lon, last.lat, last.lon) < 40) map.fitBounds(L.latLngBounds([[lat, lon], [last.lat, last.lon]]).pad(0.35), { maxZoom: 15 });
    }
  });

  /* ---------------------------------------------------------------- nearest-first lists */
  IP.register("distance-sort", function (list) {
    function apply(lat, lon) {
      var sorted = sortByDistance(list.children, lat, lon);
      sorted.forEach(function (row) {
        list.appendChild(row.el);
        if (row.km == null) return;
        var chip = row.el.querySelector("[data-distance]");
        if (chip) {
          chip.textContent = formatKm(row.km);
        } else {
          var cover = row.el.querySelector(".pub-cover");
          if (cover) {
            cover.appendChild(
              IP.h("span", { class: "pub-photo-badge absolute bottom-3 left-3" }, svgIcon("pin", "size-3.5 text-accent"), IP.h("span", { "data-distance": "", text: formatKm(row.km) }))
            );
          }
        }
      });
    }
    // server already sorted when it knew the location; otherwise use this tab's last shared location
    if (!list.querySelector("[data-distance]")) {
      var last = lastLocation();
      if (last) apply(last.lat, last.lon);
    }
    IP.on("location", function (loc) {
      apply(loc.lat, loc.lon);
    });
  });
})();

;
/* 11-account.js */
/* WP-A2 auth + account behaviour (progressive enhancement - every form also works without this file and the
 * server re-validates everything):
 *   password-field  show/hide toggle, live strength meter + checklist, "passwords match" hint
 *   signup-form     member/owner switch (photo requirement), minor -> guardian block, owner/staff age hints,
 *                   country follows the phone code, per-section completion + progress rail / mobile bar
 *   tz-hidden       fills a hidden <input name="timezone"> with the device time zone
 *   tz-select       "Use this device's time zone" + autofill when the account still says UTC
 *   photo-preview   instant preview of a chosen profile photo
 *   notif-list      mark one / all notifications read through the JSON API (keeps the island badge in sync)
 *   delete-confirm  enables the delete button only after typing DELETE
 */
(function () {
  "use strict";
  var IP = window.IP;

  var COMMON = [
    "password", "password1", "password123", "123456", "12345678", "123456789", "1234567890", "qwerty", "qwerty123",
    "111111", "abc123", "letmein", "welcome", "welcome1", "iloveyou", "admin", "admin123", "monkey", "dragon",
    "football", "baseball", "sunshine", "princess", "passw0rd", "p@ssw0rd", "gym12345", "fitness1", "ironpulse",
    "changeme", "trustno1", "superman", "batman", "000000", "987654321",
  ];
  var LEVEL_LABELS = [
    "Use 10+ characters with a mix of letters, numbers and symbols.",
    "Weak - keep going",
    "Almost there",
    "Good password",
    "Strong password",
  ];

  function each(list, fn) {
    Array.prototype.forEach.call(list || [], fn);
  }

  function ageFrom(value) {
    var m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value || "");
    if (!m) return null;
    var y = +m[1], mo = +m[2], d = +m[3];
    var now = new Date();
    var age = now.getFullYear() - y;
    if (now.getMonth() + 1 < mo || (now.getMonth() + 1 === mo && now.getDate() < d)) age -= 1;
    return age;
  }

  function personalInfo(form) {
    var scope = form || document;
    var pick = function (sel) {
      var el = scope.querySelector(sel);
      return el ? String(el.value || "") : "";
    };
    return {
      email: pick('[name="email"]') || pick("[data-personal-email]"),
      name: pick('[name="full_name"]') || pick("[data-personal-name]"),
    };
  }

  function containsPersonal(lower, info) {
    var local = (info.email || "").split("@")[0].toLowerCase();
    if (local.length >= 4 && lower.indexOf(local) !== -1) return true;
    var parts = (info.name || "").toLowerCase().split(/\s+/);
    for (var i = 0; i < parts.length; i++) {
      if (parts[i].length >= 4 && lower.indexOf(parts[i]) !== -1) return true;
    }
    return false;
  }

  /** Mirrors app.security.passwords.password_problems (the server stays the source of truth). */
  function evaluate(pw, info) {
    var classes = [/[a-z]/, /[A-Z]/, /\d/, /[^A-Za-z0-9]/].filter(function (re) {
      return re.test(pw);
    }).length;
    var lower = pw.toLowerCase();
    var rules = {
      length: pw.length >= 10 && pw.length <= 128,
      mix: classes >= 3,
      common: pw.length > 0 && COMMON.indexOf(lower) === -1,
      personal: pw.length > 0 && !containsPersonal(lower, info),
    };
    var passed = 0;
    Object.keys(rules).forEach(function (k) {
      if (rules[k]) passed += 1;
    });
    var level = 0;
    if (pw.length) {
      level = 1;
      if (passed === 4) level = pw.length >= 14 && classes === 4 ? 4 : 3;
      else if (passed === 3) level = 2;
    }
    return { rules: rules, level: level };
  }

  /* ---------------------------------------------------------------- password-field */
  IP.register("password-field", function (wrap) {
    var input = wrap.querySelector("[data-password-input]");
    if (!input) return;
    var form = input.form;
    var toggle = wrap.querySelector("[data-password-toggle]");
    if (toggle) {
      toggle.hidden = false;
      toggle.setAttribute("aria-label", "Show password");
      toggle.addEventListener("click", function () {
        var show = input.type === "password";
        input.type = show ? "text" : "password";
        toggle.textContent = show ? "Hide" : "Show";
        toggle.setAttribute("aria-pressed", show ? "true" : "false");
        toggle.setAttribute("aria-label", show ? "Hide password" : "Show password");
        input.focus();
      });
      if (form) {
        form.addEventListener("submit", function () {
          input.type = "password"; // never leave a revealed password on screen after submitting
        });
      }
    }

    if (wrap.getAttribute("data-strength") === "1") {
      var meter = wrap.querySelector("[data-strength-meter]");
      var bar = wrap.querySelector("[data-strength-bar]");
      var label = wrap.querySelector("[data-strength-label]");
      var rules = wrap.querySelectorAll("[data-rule]");
      var help = wrap.querySelector(".help");
      if (meter) meter.hidden = false;
      if (help && meter) help.hidden = true;
      var update = function () {
        var res = evaluate(input.value, personalInfo(form));
        if (bar) {
          bar.setAttribute("data-level", String(res.level));
          bar.style.width = res.level * 25 + "%";
        }
        if (label) label.textContent = LEVEL_LABELS[res.level];
        each(rules, function (li) {
          li.setAttribute("data-ok", res.rules[li.getAttribute("data-rule")] ? "1" : "0");
        });
        input.setCustomValidity(input.value && res.level < 3 ? "Choose a stronger password - see the checklist below the field." : "");
      };
      input.addEventListener("input", update);
      if (form) {
        form.addEventListener("input", function (e) {
          var n = e.target && e.target.name;
          if (n === "email" || n === "full_name") update();
        });
      }
      update();
    }

    var matchName = wrap.getAttribute("data-match");
    if (matchName && form) {
      var other = form.querySelector('[name="' + matchName + '"]');
      var hint = wrap.querySelector("[data-match-hint]");
      var check = function () {
        if (!input.value) {
          if (hint) hint.hidden = true;
          input.setCustomValidity("");
          return;
        }
        var ok = !!other && input.value === other.value;
        if (hint) {
          hint.hidden = false;
          hint.setAttribute("data-ok", ok ? "1" : "0");
          hint.textContent = ok ? "Passwords match" : "Passwords don't match yet";
        }
        input.setCustomValidity(ok ? "" : "The two passwords don't match.");
      };
      input.addEventListener("input", check);
      if (other) other.addEventListener("input", check);
    }
  });

  /* ---------------------------------------------------------------- tz-hidden / tz-select */
  IP.register("tz-hidden", function (el) {
    if (!el.value && IP.deviceTimezone) el.value = IP.deviceTimezone();
  });

  IP.register("tz-select", function (el) {
    var select = el.querySelector("[data-tz-input]");
    var btn = el.querySelector("[data-tz-device]");
    var hint = el.querySelector("[data-tz-hint]");
    var tz = IP.deviceTimezone ? IP.deviceTimezone() : "";
    if (!select || !tz) return;
    var ensure = function () {
      var found = false;
      each(select.options, function (o) {
        if (o.value === tz) found = true;
      });
      if (!found) select.appendChild(IP.h("option", { value: tz, text: tz }));
    };
    var apply = function (message) {
      ensure();
      select.value = tz;
      if (btn) btn.hidden = true;
      if (hint) hint.textContent = message;
    };
    if (btn && select.value !== tz) {
      btn.hidden = false;
      btn.textContent = "Use " + tz.replace(/_/g, " ") + " (this device)";
      btn.addEventListener("click", function () {
        apply("Set to this device's time zone - save to apply.");
      });
    }
    if (el.getAttribute("data-autofill") === "1" && (!select.value || select.value === "UTC") && tz !== "UTC") {
      apply("We picked " + tz.replace(/_/g, " ") + " from this device - save to apply.");
    }
  });

  /* ---------------------------------------------------------------- photo-preview */
  IP.register("photo-preview", function (el) {
    var input = el.querySelector("[data-photo-input]");
    var img = el.querySelector("[data-photo-img]");
    var placeholder = el.querySelector("[data-photo-placeholder]");
    if (!input || !img) return;
    var url = null;
    input.addEventListener("change", function () {
      var file = input.files && input.files[0];
      if (url) {
        URL.revokeObjectURL(url);
        url = null;
      }
      input.setCustomValidity("");
      if (!file) return;
      if (!/^image\/(jpeg|png|webp)$/.test(file.type)) {
        input.setCustomValidity("Choose a JPG, PNG or WEBP image.");
        input.reportValidity();
        return;
      }
      url = URL.createObjectURL(file);
      img.src = url;
      img.classList.remove("hidden");
      if (placeholder) placeholder.hidden = true;
    });
  });

  /* ---------------------------------------------------------------- signup-form (also the staff-invite form) */
  IP.register("signup-form", function (form) {
    var isStaff = form.getAttribute("data-staff") === "1";
    var dob = form.querySelector('[name="date_of_birth"]');
    var radios = form.querySelectorAll("[data-role-radio]");
    var guardianBlock = form.querySelector("[data-guardian-block]");
    var guardianFieldset = form.querySelector("[data-guardian-fieldset]");
    var guardianStep = document.querySelector("[data-guardian-step]");
    var photo = form.querySelector("[data-photo-input]");
    var photoMark = form.querySelector("[data-photo-required-mark]");
    var photoHelps = form.querySelectorAll("[data-photo-help]");
    var rail = document.querySelector("[data-signup-steps]");
    var mobile = form.querySelector("[data-signup-mobile-progress]");
    var sections = Array.prototype.slice.call(form.querySelectorAll("section[data-step]"));

    var role = function () {
      var r = form.querySelector("[data-role-radio]:checked");
      return r ? r.value : isStaff ? "staff" : "customer";
    };

    function syncRules() {
      var r = role();
      var age = dob ? ageFrom(dob.value) : null;
      var minor = !isStaff && r === "customer" && age !== null && age >= 0 && age < 18;
      if (guardianBlock) {
        var wasHidden = guardianBlock.hidden;
        guardianBlock.hidden = !minor;
        if (guardianFieldset) guardianFieldset.disabled = !minor;
        if (guardianStep) guardianStep.hidden = !minor;
        if (minor && wasHidden) {
          guardianBlock.classList.remove("animate-fade-up");
          void guardianBlock.offsetWidth; // restart the entrance animation
          guardianBlock.classList.add("animate-fade-up");
        }
      }
      if (!isStaff && photo) {
        var owner = r === "owner";
        photo.required = owner;
        if (photoMark) photoMark.hidden = !owner;
        each(photoHelps, function (h) {
          h.hidden = h.getAttribute("data-photo-help") !== (owner ? "owner" : "customer");
        });
      }
      if (dob) {
        var msg = "";
        if (age !== null && age < 0) msg = "Your date of birth can't be in the future.";
        else if (isStaff && age !== null && age < 16) msg = "Staff members must be at least 16 years old.";
        else if (!isStaff && r === "owner" && age !== null && age < 18) msg = "Gym owners must be at least 18 years old.";
        else if (age !== null && age < 5) msg = "You must be at least 5 years old.";
        dob.setCustomValidity(msg);
      }
    }

    function visibleSections() {
      return sections.filter(function (s) {
        return !s.hidden;
      });
    }

    function complete(section) {
      var ok = true;
      each(section.querySelectorAll("input, select, textarea"), function (f) {
        if (!ok || f.disabled || f.type === "hidden") return;
        if (!f.checkValidity()) ok = false;
      });
      return ok;
    }

    function titleOf(section) {
      var h = section.querySelector("h2");
      return h ? h.textContent.trim() : "";
    }

    var current = null;
    function refreshProgress() {
      var vis = visibleSections();
      var done = 0;
      vis.forEach(function (s) {
        var ok = complete(s);
        if (ok) done += 1;
        s.setAttribute("data-complete", ok ? "1" : "0");
        if (rail) {
          var link = rail.querySelector('[data-step-link="' + s.getAttribute("data-step") + '"]');
          if (link) link.setAttribute("data-done", ok ? "1" : "0");
        }
      });
      if (mobile && vis.length) {
        mobile.hidden = false;
        var bar = mobile.querySelector("[data-progress-bar]");
        var count = mobile.querySelector("[data-progress-count]");
        var label = mobile.querySelector("[data-progress-label]");
        if (bar) bar.style.width = Math.round((done / vis.length) * 100) + "%";
        if (count) count.textContent = done + " of " + vis.length + " done";
        if (label && current) label.textContent = titleOf(current);
      }
    }

    var queued = false;
    function schedule() {
      if (queued) return;
      queued = true;
      window.requestAnimationFrame(function () {
        queued = false;
        syncRules();
        refreshProgress();
      });
    }

    each(radios, function (r) {
      r.addEventListener("change", schedule);
    });
    form.addEventListener("input", schedule);
    form.addEventListener("change", schedule);

    // the country follows the phone's calling code until the person picks a country themselves
    var phoneCountry = form.querySelector('select[name="phone_country"]');
    var country = form.querySelector("[data-country-select]");
    var idCountry = form.querySelector('[name="id_country"]');
    var countryTouched = false;
    var idTouched = false;
    if (country) {
      country.addEventListener("change", function (e) {
        if (e.isTrusted) countryTouched = true;
        if (idCountry && !idTouched) idCountry.value = country.value;
      });
    }
    if (idCountry) {
      idCountry.addEventListener("input", function () {
        idTouched = true;
      });
    }
    if (phoneCountry && country) {
      phoneCountry.addEventListener("change", function () {
        if (countryTouched) return;
        var has = false;
        each(country.options, function (o) {
          if (o.value === phoneCountry.value) has = true;
        });
        if (has) {
          country.value = phoneCountry.value;
          if (idCountry && !idTouched) idCountry.value = phoneCountry.value;
        }
      });
    }

    // highlight the section in view on the progress rail
    if ("IntersectionObserver" in window && sections.length) {
      var io = new IntersectionObserver(
        function (entries) {
          entries.forEach(function (en) {
            if (!en.isIntersecting) return;
            current = en.target;
            var key = current.getAttribute("data-step");
            if (rail) {
              each(rail.querySelectorAll("[data-step-link]"), function (a) {
                if (a.getAttribute("data-step-link") === key) a.setAttribute("data-state", "current");
                else a.removeAttribute("data-state");
              });
            }
            refreshProgress();
          });
        },
        { rootMargin: "-30% 0px -60% 0px" }
      );
      sections.forEach(function (s) {
        io.observe(s);
      });
    }

    syncRules();
    refreshProgress();
  });

  /* ---------------------------------------------------------------- notif-list */
  IP.register("notif-list", function (root) {
    var totalEl = root.querySelector("[data-unread-total]");
    var readAll = document.querySelector("[data-notif-read-all]");

    function setUnread(n) {
      n = Math.max(0, n | 0);
      if (totalEl) totalEl.textContent = String(n);
      each(document.querySelectorAll("[data-unread-count]"), function (b) {
        b.textContent = String(n);
        b.hidden = n === 0;
      });
      if (readAll) readAll.hidden = n === 0;
      if (IP.island && IP.island.setUnread) IP.island.setUnread(n);
    }

    function markRow(row) {
      if (!row) return;
      row.setAttribute("data-unread", "0");
      var badge = row.querySelector("[data-new-badge]");
      if (badge) badge.remove();
      var f = row.querySelector("[data-notif-read]");
      if (f) f.remove();
    }

    root.addEventListener("submit", function (e) {
      var form = e.target.closest && e.target.closest("[data-notif-read]");
      if (!form) return;
      e.preventDefault();
      IP.api("/api/notifications/" + encodeURIComponent(form.getAttribute("data-id")) + "/read", { method: "POST" })
        .then(function (d) {
          markRow(form.closest("[data-notif]"));
          setUnread(d.unread || 0);
        })
        .catch(function () {
          form.submit();
        });
    });

    if (readAll) {
      readAll.addEventListener("submit", function (e) {
        e.preventDefault();
        IP.api("/api/notifications/read-all", { method: "POST" })
          .then(function () {
            each(root.querySelectorAll('[data-notif][data-unread="1"]'), markRow);
            setUnread(0);
            if (IP.island) IP.island.notify({ title: "All caught up", body: "Every notification is marked as read.", tone: "success", timeout: 2200 });
          })
          .catch(function () {
            readAll.submit();
          });
      });
    }

    IP.on("notification", function () {
      if (!totalEl) return;
      setUnread((parseInt(totalEl.textContent, 10) || 0) + 1);
    });
  });

  /* ---------------------------------------------------------------- delete-confirm */
  IP.register("delete-confirm", function (form) {
    var input = form.querySelector("[data-confirm-input]");
    var btn = form.querySelector("[data-confirm-submit]");
    if (!input || !btn) return;
    var sync = function () {
      var ok = input.value.trim() === "DELETE";
      btn.disabled = !ok;
      btn.setAttribute("aria-disabled", ok ? "false" : "true");
    };
    input.addEventListener("input", sync);
    sync();
  });
})();

;
/* 12-identity.js */
/* WP-H identity cards (progressive enhancement - every form works without this file and the server re-validates):
 *   id-upload   drag & drop + instant preview on the ID-card file inputs, type/size hints, passport tip,
 *               upper-cases the country code (used by signup, staff invite and /account/identity)
 *   idv-reveal  privacy blur on card images: "Show" / "Hide"
 *   idv-live    while a card is being checked: refresh on a live "verification" notification, or poll
 *               GET /api/identity/status (3 s -> 10 s back-off, stops after 10 minutes) and reload when done
 *   idv-review  owner / admin review form: note required when rejecting, in-person confirmation when approving
 */
(function () {
  "use strict";
  var IP = window.IP;
  var TYPES = ["image/jpeg", "image/png", "image/webp"];

  function each(list, fn) {
    Array.prototype.forEach.call(list || [], fn);
  }

  function fmtSize(bytes) {
    if (bytes >= 1048576) return (bytes / 1048576).toFixed(1) + " MB";
    return Math.max(1, Math.round(bytes / 1024)) + " KB";
  }

  /* ---------------------------------------------------------------- id-upload */
  IP.register("id-upload", function (fieldset) {
    var maxMb = parseFloat(fieldset.getAttribute("data-max-mb") || "10") || 10;
    var docType = fieldset.querySelector("[data-doc-type]");
    var country = fieldset.querySelector("[data-country]");
    var passportHint = fieldset.querySelector("[data-passport-hint]");

    function syncHint() {
      if (!passportHint || !docType) return;
      var on = docType.value === "passport";
      passportHint.classList.toggle("hidden", !on);
      passportHint.classList.toggle("flex", on);
    }
    if (docType) {
      docType.addEventListener("change", syncHint);
      syncHint();
    }
    if (country) {
      country.addEventListener("input", function () {
        var v = country.value.replace(/[^a-z]/gi, "").toUpperCase().slice(0, 2);
        if (v !== country.value) country.value = v;
      });
    }

    each(fieldset.querySelectorAll("[data-drop]"), function (zone) {
      var input = zone.querySelector('input[type="file"]');
      var preview = zone.querySelector("[data-preview]");
      var empty = zone.querySelector("[data-empty]");
      var hint = zone.querySelector("[data-drop-hint]");
      var errorEl = zone.parentElement ? zone.parentElement.querySelector("[data-client-error]") : null;
      var defaultHint = hint ? hint.textContent : "";
      var url = null;
      if (!input) return;

      function setError(msg) {
        if (!errorEl) return;
        errorEl.textContent = msg || "";
        errorEl.classList.toggle("hidden", !msg);
        if (msg) input.setAttribute("aria-invalid", "true");
        else input.removeAttribute("aria-invalid");
      }

      function reset() {
        if (url) URL.revokeObjectURL(url);
        url = null;
        if (preview) {
          preview.classList.add("hidden");
          preview.removeAttribute("src");
        }
        if (empty) empty.classList.remove("hidden");
        if (hint) hint.textContent = defaultHint;
        zone.removeAttribute("data-filled");
      }

      function show(file) {
        setError("");
        if (!file) return reset();
        if (TYPES.indexOf(file.type) === -1) {
          input.value = "";
          reset();
          return setError("Please choose a JPG, PNG or WEBP photo of the card.");
        }
        if (file.size > maxMb * 1048576) {
          input.value = "";
          reset();
          return setError("That photo is " + fmtSize(file.size) + " - the limit is " + maxMb + " MB.");
        }
        if (url) URL.revokeObjectURL(url);
        url = URL.createObjectURL(file);
        if (preview) {
          preview.src = url;
          preview.alt = "Preview of " + file.name;
          preview.classList.remove("hidden");
        }
        if (empty) empty.classList.add("hidden");
        if (hint) hint.textContent = file.name + " · " + fmtSize(file.size);
        zone.setAttribute("data-filled", "1");
      }

      input.addEventListener("change", function () {
        show(input.files && input.files[0]);
      });
      ["dragenter", "dragover"].forEach(function (type) {
        zone.addEventListener(type, function (e) {
          e.preventDefault();
          zone.setAttribute("data-dragging", "1");
        });
      });
      ["dragleave", "dragend", "drop"].forEach(function (type) {
        zone.addEventListener(type, function () {
          zone.removeAttribute("data-dragging");
        });
      });
      zone.addEventListener("drop", function (e) {
        e.preventDefault();
        var files = e.dataTransfer && e.dataTransfer.files;
        if (!files || !files.length) return;
        try {
          var dt = new DataTransfer();
          dt.items.add(files[0]);
          input.files = dt.files;
        } catch (err) {
          return setError("Your browser can't drop files here - use the button instead.");
        }
        show(files[0]);
      });
    });
  });

  /* ---------------------------------------------------------------- idv-reveal */
  IP.register("idv-reveal", function (fig) {
    var btn = fig.querySelector("[data-reveal]");
    var label = fig.querySelector("[data-reveal-label]");
    var img = fig.querySelector("img");
    if (img) {
      img.addEventListener("error", function () {
        fig.classList.add("hidden");
      });
    }
    if (!btn) return;
    btn.addEventListener("click", function () {
      var hidden = fig.getAttribute("data-hidden") === "1";
      fig.setAttribute("data-hidden", hidden ? "0" : "1");
      btn.setAttribute("aria-pressed", hidden ? "true" : "false");
      if (label) label.textContent = hidden ? "Hide" : "Show";
    });
  });

  /* ---------------------------------------------------------------- idv-live */
  IP.register("idv-live", function (el) {
    var url = el.getAttribute("data-url");
    var docId = el.getAttribute("data-doc");
    var status = el.getAttribute("data-status");
    var text = el.querySelector("[data-live-text]");
    var started = Date.now();
    var delay = 3000;
    var stopped = false;
    var timer = null;

    function reload() {
      if (stopped) return;
      stopped = true;
      if (text) text.textContent = "Result ready - updating…";
      setTimeout(function () {
        window.location.reload();
      }, 400);
    }

    function tick() {
      if (stopped) return;
      if (Date.now() - started > 10 * 60 * 1000) {
        if (text) text.textContent = "Still working on it - refresh the page in a little while.";
        return;
      }
      IP.api(url)
        .then(function (data) {
          if (!data) return;
          if (String(data.document_id) !== String(docId) || data.document_status !== status) {
            if (data.done || data.document_status !== status) return reload();
          }
          var secs = Math.round((Date.now() - started) / 1000);
          if (text && secs >= 20) text.textContent = "Still reading your card (" + secs + " s) - several engines are cross-checking it.";
        })
        .catch(function () {
          /* offline / signed out: back off quietly */
        })
        .then(function () {
          delay = Math.min(10000, Math.round(delay * 1.4));
          if (!stopped && !document.hidden) timer = setTimeout(tick, delay);
        });
    }

    IP.on("notification", function (evt) {
      if (evt && (evt.category === "verification" || /identity|id card/i.test(evt.title || ""))) reload();
    });
    document.addEventListener("visibilitychange", function () {
      if (!document.hidden && !stopped) {
        clearTimeout(timer);
        delay = 3000;
        tick();
      }
    });
    timer = setTimeout(tick, delay);
  });

  /* ---------------------------------------------------------------- idv-review */
  IP.register("idv-review", function (form) {
    var note = form.querySelector("[data-review-note]");
    var seen = form.querySelector("[data-review-seen]");
    var noteLabel = form.querySelector("[data-note-required]");
    function sync() {
      var picked = form.querySelector('input[name="decision"]:checked');
      var value = picked ? picked.value : "";
      if (note) note.required = value === "reject";
      if (noteLabel) noteLabel.classList.toggle("hidden", value !== "reject");
      if (seen) seen.required = value === "approve";
    }
    each(form.querySelectorAll('input[name="decision"]'), function (r) {
      r.addEventListener("change", sync);
    });
    sync();
  });
})();

;
/* 20-owner.js */
/* WP-B owner console behaviour (no inline JS - strict CSP).
 *   data-component="country-currency"     gym form: country -> default currency, live logo preview
 *   data-component="hours-editor"         opening hours: closed toggles, live duration, 14 h guard, copy Monday
 *   data-component="verified-guard"       verified branch form: confirm before address/pin changes reset verification
 *   data-component="verification-status"  live refresh while a branch is queued/processing (poll + websocket)
 *   data-component="photo-picker"         photo previews, remaining-slot check, drag & drop
 *   data-reg-country / data-reg-labels    registration-number label follows the selected country
 */
(function () {
  "use strict";
  var IP = window.IP;
  if (!IP) return;

  function toast(title, body, tone) {
    if (IP.island && IP.island.notify) IP.island.notify({ title: title, body: body || "", tone: tone || "info", timeout: 3200 });
  }

  function minutesOf(value) {
    var m = /^(\d{1,2}):(\d{2})/.exec(value || "");
    return m ? parseInt(m[1], 10) * 60 + parseInt(m[2], 10) : null;
  }

  /* ------------------------------------------------------------------ gym form */
  IP.register("country-currency", function (form) {
    var map = {};
    try {
      map = JSON.parse(form.getAttribute("data-currency-map") || "{}");
    } catch (e) {
      map = {};
    }
    var country = form.querySelector("[data-country-select]");
    var currency = form.querySelector("[data-currency-select]");
    if (country && currency) {
      country.addEventListener("change", function () {
        var cur = map[country.value];
        if (cur && currency.querySelector('option[value="' + cur + '"]')) currency.value = cur;
      });
    }
    var logo = form.querySelector('input[name="logo"]');
    var preview = form.querySelector("[data-logo-preview]");
    if (logo && preview && window.URL && URL.createObjectURL) {
      logo.addEventListener("change", function () {
        var file = logo.files && logo.files[0];
        if (!file || !/^image\//.test(file.type)) return;
        preview.textContent = "";
        preview.appendChild(IP.h("img", { src: URL.createObjectURL(file), alt: "New logo preview", class: "size-full object-cover" }));
      });
    }
  });

  /* ------------------------------------------------------------------ registration label follows the country */
  function wireRegistration(root) {
    var holder = root.querySelector("[data-reg-labels]");
    var select = root.querySelector("[data-reg-country]");
    if (!holder || !select) return;
    var labels = {};
    try {
      labels = JSON.parse(holder.getAttribute("data-reg-labels") || "{}");
    } catch (e) {
      return;
    }
    select.addEventListener("change", function () {
      var info = labels[select.value] || labels["*"];
      if (!info) return;
      var set = function (sel, text) {
        var el = holder.querySelector(sel);
        if (el) el.textContent = text;
      };
      set("[data-reg-label]", info.label);
      set("[data-reg-example]", info.example);
      set("[data-reg-authority]", info.authority);
      var input = holder.querySelector("[data-reg-input]");
      if (input) input.setAttribute("placeholder", info.example);
    });
  }

  /* ------------------------------------------------------------------ opening hours */
  IP.register("hours-editor", function (root) {
    var maxMinutes = Math.round(parseFloat(root.getAttribute("data-max-hours") || "14") * 60);
    var rows = Array.prototype.slice.call(root.querySelectorAll("[data-day]"));
    wireRegistration(root.closest("form") || document);

    function update(row) {
      var closed = row.querySelector("[data-closed]");
      var open = row.querySelector("[data-open]");
      var close = row.querySelector("[data-close]");
      var out = row.querySelector("[data-duration]");
      var isClosed = closed && closed.checked;
      row.setAttribute("data-closed", isClosed ? "1" : "0");
      [open, close].forEach(function (inp) {
        if (inp) inp.tabIndex = isClosed ? -1 : 0;
      });
      if (!out) return;
      if (isClosed) {
        out.textContent = "";
        out.removeAttribute("data-over");
        return;
      }
      var a = minutesOf(open && open.value);
      var b = minutesOf(close && close.value);
      if (a === null || b === null) {
        out.textContent = "";
        out.removeAttribute("data-over");
        return;
      }
      var span = b - a;
      if (span <= 0) {
        out.textContent = "invalid";
        out.setAttribute("data-over", "1");
        return;
      }
      var h = Math.floor(span / 60);
      var m = span % 60;
      out.textContent = h + " h" + (m ? " " + (m < 10 ? "0" : "") + m : "");
      if (span > maxMinutes) out.setAttribute("data-over", "1");
      else out.removeAttribute("data-over");
    }

    rows.forEach(function (row) {
      row.addEventListener("input", function () {
        update(row);
      });
      row.addEventListener("change", function () {
        update(row);
      });
      update(row);
    });

    var copy = root.querySelector("[data-copy-first]");
    if (copy && rows.length) {
      copy.addEventListener("click", function () {
        var first = rows[0];
        var o = first.querySelector("[data-open]").value;
        var c = first.querySelector("[data-close]").value;
        var closed = first.querySelector("[data-closed]").checked;
        rows.slice(1).forEach(function (row) {
          row.querySelector("[data-open]").value = o;
          row.querySelector("[data-close]").value = c;
          row.querySelector("[data-closed]").checked = closed;
          update(row);
        });
        toast("Hours copied", "Monday's hours now apply to every day.", "success");
      });
    }
  });

  /* ------------------------------------------------------------------ verified branch guard */
  IP.register("verified-guard", function (form) {
    if (form.getAttribute("data-verified") !== "1") return;
    var fields = Array.prototype.slice.call(form.querySelectorAll("[data-material]"));
    var initial = fields.map(function (f) {
      return (f.value || "").trim().toLowerCase();
    });
    form.addEventListener("submit", function (e) {
      var changed = fields.some(function (f, i) {
        return (f.value || "").trim().toLowerCase() !== initial[i];
      });
      var box = form.querySelector("[data-confirm-reverify]");
      if (!changed || !box || box.checked) return;
      var ok = window.confirm(
        "You changed the address, registration number or map pin of a verified branch.\n\n" +
          "Saving resets it to draft and hides it from the website until it is verified again. Continue?"
      );
      if (ok) {
        box.checked = true;
      } else {
        e.preventDefault();
        e.stopPropagation(); // keep the global "busy" handler from disabling the button
      }
    });
  });

  /* ------------------------------------------------------------------ live verification status */
  IP.register("verification-status", function (el) {
    var status = el.getAttribute("data-status");
    var url = el.getAttribute("data-url");
    var branchId = el.getAttribute("data-branch-id");
    if (!url || (status !== "queued" && status !== "processing")) return;
    var started = Date.now();
    var timer = null;
    var reloading = false;

    function reload() {
      if (reloading) return;
      reloading = true;
      if (timer) clearTimeout(timer);
      if (location.hash !== "#verification") history.replaceState(null, "", location.pathname + location.search + "#verification");
      location.reload();
    }

    function poll() {
      IP.api(url)
        .then(function (data) {
          if (!data) return;
          var title = el.querySelector("[data-status-title]");
          if (data.status === "processing" && title && status === "queued") title.textContent = "Checking your documents…";
          if (data.status !== "queued" && data.status !== "processing") return reload();
          schedule();
        })
        .catch(function () {
          schedule(true);
        });
    }

    function schedule(slow) {
      if (Date.now() - started > 15 * 60 * 1000) return; // give up after 15 minutes
      timer = setTimeout(poll, slow ? 10000 : 4000);
    }

    IP.on("branch.verification", function (evt) {
      if (evt && String(evt.branch_id) === String(branchId) && evt.status !== "queued" && evt.status !== "processing") reload();
    });
    IP.on("notification", function (evt) {
      if (evt && evt.category === "verification" && evt.link && evt.link.indexOf("/owner/branches/" + branchId) === 0) reload();
    });
    schedule();
  });

  /* ------------------------------------------------------------------ photo picker */
  IP.register("photo-picker", function (form) {
    var input = form.querySelector("[data-photo-input]");
    var previews = form.querySelector("[data-photo-previews]");
    var zone = form.querySelector(".owner-dropzone");
    var remaining = parseInt(form.getAttribute("data-remaining") || "12", 10);
    if (!input) return;

    function render() {
      if (!previews) return;
      previews.textContent = "";
      var files = Array.prototype.slice.call(input.files || []);
      files.forEach(function (file, i) {
        if (!/^image\//.test(file.type)) return;
        var over = i >= remaining;
        var thumb = IP.h(
          "div",
          { class: "owner-thumb" + (over ? " opacity-40" : "") },
          IP.h("img", { src: URL.createObjectURL(file), alt: file.name })
        );
        previews.appendChild(thumb);
      });
      if (files.length > remaining) {
        toast("Too many photos", "Only " + remaining + " more photo" + (remaining === 1 ? "" : "s") + " fit on this branch.", "warning");
      }
    }

    input.addEventListener("change", render);
    if (zone) {
      ["dragenter", "dragover"].forEach(function (type) {
        zone.addEventListener(type, function (e) {
          e.preventDefault();
          zone.setAttribute("data-drag", "1");
        });
      });
      ["dragleave", "drop"].forEach(function (type) {
        zone.addEventListener(type, function () {
          zone.removeAttribute("data-drag");
        });
      });
      zone.addEventListener("drop", function (e) {
        e.preventDefault();
        if (e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files.length) {
          try {
            input.files = e.dataTransfer.files;
            render();
          } catch (err) {
            toast("Drag & drop unsupported", "Click the box to choose photos instead.", "warning");
          }
        }
      });
    }
    form.addEventListener("submit", function (e) {
      if (input.files && input.files.length > remaining) {
        e.preventDefault();
        e.stopPropagation();
        toast("Too many photos", "Choose at most " + remaining + ".", "warning");
      }
    });
  });

  // registration label on the "new branch" page (no hours-editor wrapper around it)
  document.addEventListener("DOMContentLoaded", function () {
    if (!document.querySelector('[data-component~="hours-editor"]')) wireRegistration(document);
  });
})();

;
/* 21-map.js */
/* WP-B maps (Leaflet is vendored at /static/vendor/leaflet/ and loaded only on pages that need it).
 *   data-component="branch-picker"  draggable pin + "Find address" (GET /api/geo/search) + "Use my location",
 *                                   keeps the lat/lon inputs ([data-lat] / [data-lon]) of the form in sync.
 *   data-component="map-view"       read-only map: data-points='{"pin":[lat,lon],"geocoded":[lat,lon],"label":"..."}'
 * Tiles come from the same-origin proxy /maps/tiles/{z}/{x}/{y}.png (Geoapify, OpenStreetMap fallback).
 */
(function () {
  "use strict";
  var IP = window.IP;
  if (!IP) return;

  var TILE_URL = "/maps/tiles/{z}/{x}/{y}.png";

  function attribution(geoapify) {
    return (
      '&copy; <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener">OpenStreetMap</a> contributors' +
      (geoapify ? ' &middot; <a href="https://www.geoapify.com/" target="_blank" rel="noopener">Geoapify</a>' : "")
    );
  }

  function pinIcon(alt) {
    return window.L.divIcon({
      className: "owner-pin-wrap",
      html: '<span class="owner-pin' + (alt ? " owner-pin-alt" : "") + '"></span>',
      iconSize: [30, 30],
      iconAnchor: [15, 15],
    });
  }

  function round6(n) {
    return Math.round(n * 1e6) / 1e6;
  }

  function validLatLon(lat, lon) {
    return isFinite(lat) && isFinite(lon) && lat >= -90 && lat <= 90 && lon >= -180 && lon <= 180 && !(lat === 0 && lon === 0);
  }

  /** Run init now if the element is visible, otherwise when its closed <details> opens (Leaflet needs a size). */
  function whenVisible(el, init) {
    var details = el.closest("details");
    if (details && !details.open) {
      var onToggle = function () {
        if (!details.open) return;
        details.removeEventListener("toggle", onToggle);
        setTimeout(init, 30);
      };
      details.addEventListener("toggle", onToggle);
      return;
    }
    init();
  }

  function leafletMissing(container) {
    var fb = container.querySelector(".owner-map-fallback");
    if (fb) {
      fb.textContent = "";
      fb.appendChild(IP.h("p", { class: "text-sm font-medium", text: "Map unavailable" }));
      fb.appendChild(IP.h("p", { class: "text-xs", text: "Type the latitude and longitude below instead." }));
    }
  }

  function baseMap(container, opts) {
    var fb = container.querySelector(".owner-map-fallback");
    if (fb) fb.remove();
    var map = window.L.map(container, {
      zoomControl: true,
      scrollWheelZoom: false,
      attributionControl: true,
      worldCopyJump: true,
    });
    window.L.tileLayer(TILE_URL, { maxZoom: 19, minZoom: 2, attribution: attribution(opts.geoapify) }).addTo(map);
    map.attributionControl.setPrefix(false);
    return map;
  }

  /* ------------------------------------------------------------------ editable pin */
  IP.register("branch-picker", function (root) {
    var container = root.querySelector("[data-map]");
    var form = root.closest("form") || document;
    var latInput = root.querySelector("[data-lat]");
    var lonInput = root.querySelector("[data-lon]");
    var status = root.querySelector("[data-picker-status]");
    var results = root.querySelector("[data-results]");
    var findBtn = root.querySelector("[data-find-address]");
    var locateBtn = root.querySelector("[data-locate]");
    var geoapify = root.getAttribute("data-geoapify") === "1";
    var center = [20, 10, 2];
    try {
      center = JSON.parse(root.getAttribute("data-center") || "[20,10,2]");
    } catch (e) {
      /* keep default */
    }
    var map = null;
    var marker = null;

    function say(text) {
      if (status) status.textContent = text;
    }

    function field(name) {
      return form.querySelector('[name="' + name + '"]');
    }

    function fillIfEmpty(name, value) {
      var f = field(name);
      if (f && value && !(f.value || "").trim()) {
        f.value = value;
        f.dispatchEvent(new Event("change", { bubbles: true }));
      }
    }

    function setInputs(lat, lon) {
      if (latInput) latInput.value = round6(lat).toFixed(6);
      if (lonInput) lonInput.value = round6(lon).toFixed(6);
      [latInput, lonInput].forEach(function (inp) {
        if (inp) inp.dispatchEvent(new Event("change", { bubbles: true }));
      });
    }

    function placeMarker(lat, lon, zoom) {
      if (!map) return;
      var ll = window.L.latLng(lat, lon);
      if (!marker) {
        marker = window.L.marker(ll, { draggable: true, icon: pinIcon(false), keyboard: true, title: "Branch entrance", alt: "Branch pin" }).addTo(map);
        marker.on("dragend", function () {
          var p = marker.getLatLng();
          setInputs(p.lat, p.lng);
          say("Pin moved - remember to save.");
        });
      } else {
        marker.setLatLng(ll);
      }
      map.setView(ll, zoom || Math.max(map.getZoom(), 16), { animate: true });
    }

    function init() {
      if (!window.L) return leafletMissing(container);
      map = baseMap(container, { geoapify: geoapify });
      var lat = parseFloat(latInput && latInput.value);
      var lon = parseFloat(lonInput && lonInput.value);
      if (validLatLon(lat, lon)) {
        map.setView([lat, lon], 16);
        placeMarker(lat, lon, 16);
      } else {
        map.setView([center[0], center[1]], center[2] || 5);
      }
      map.on("click", function (e) {
        placeMarker(e.latlng.lat, e.latlng.lng);
        setInputs(e.latlng.lat, e.latlng.lng);
        say("Pin placed - drag it to fine-tune, then save.");
      });
      setTimeout(function () {
        map.invalidateSize();
      }, 200);
    }

    // typing coordinates moves the pin
    [latInput, lonInput].forEach(function (inp) {
      if (!inp) return;
      inp.addEventListener("change", function () {
        var lat = parseFloat(latInput.value);
        var lon = parseFloat(lonInput.value);
        if (map && validLatLon(lat, lon)) placeMarker(lat, lon);
      });
    });

    function addressQuery() {
      var parts = [];
      ["address_line1", "address_line2", "city", "state", "postal_code"].forEach(function (name) {
        var f = field(name);
        if (f && (f.value || "").trim()) parts.push(f.value.trim());
      });
      var country = field("country_code");
      if (country && country.selectedIndex >= 0 && country.options[country.selectedIndex]) {
        parts.push(country.options[country.selectedIndex].text);
      }
      return parts.join(", ");
    }

    function choose(r) {
      if (results) {
        results.hidden = true;
        results.textContent = "";
      }
      if (!map) init();
      placeMarker(r.lat, r.lon, 17);
      setInputs(r.lat, r.lon);
      fillIfEmpty("city", r.city);
      fillIfEmpty("state", r.state);
      fillIfEmpty("postal_code", r.postcode);
      var tz = field("timezone");
      if (tz && r.timezone && tz.value !== r.timezone) {
        tz.value = r.timezone;
      }
      say("Pin set from “" + (r.label || "search result") + "”. Drag it to the exact entrance, then save.");
    }

    function showResults(list) {
      if (!results) return;
      results.textContent = "";
      list.forEach(function (r) {
        var sub = [r.city, r.state, r.postcode, (r.country_code || "").toUpperCase()].filter(Boolean).join(" · ");
        var btn = IP.h(
          "button",
          { type: "button", onclick: function () { choose(r); } },
          IP.h("span", { class: "mt-0.5 shrink-0 text-accent", text: "●" }),
          IP.h("span", { class: "min-w-0" },
            IP.h("span", { class: "block truncate font-medium", text: r.label || "Unnamed place" }),
            IP.h("span", { class: "block truncate text-xs text-zinc-500 dark:text-zinc-400", text: sub })
          )
        );
        results.appendChild(IP.h("li", {}, btn));
      });
      results.hidden = list.length === 0;
    }

    if (findBtn) {
      findBtn.addEventListener("click", function () {
        var q = addressQuery();
        if (q.length < 3) {
          say("Type the street and city first.");
          var l1 = field("address_line1");
          if (l1) l1.focus();
          return;
        }
        findBtn.disabled = true;
        say("Searching…");
        IP.api("/api/geo/search?q=" + encodeURIComponent(q.slice(0, 200)))
          .then(function (data) {
            var list = (data && data.results) || [];
            if (!list.length) {
              say("No match for that address - click the map to drop the pin yourself.");
              showResults([]);
              return;
            }
            if (list.length === 1) return choose(list[0]);
            showResults(list);
            say("Pick the matching address:");
          })
          .catch(function (err) {
            say(err.status === 401 ? "Please sign in again." : "Address search is unavailable - click the map instead.");
          })
          .then(function () {
            findBtn.disabled = false;
          });
      });
    }

    if (locateBtn) {
      locateBtn.addEventListener("click", function () {
        if (!navigator.geolocation) return say("Your browser can't share its location.");
        locateBtn.disabled = true;
        say("Locating…");
        navigator.geolocation.getCurrentPosition(
          function (pos) {
            locateBtn.disabled = false;
            var lat = pos.coords.latitude;
            var lon = pos.coords.longitude;
            if (!map) init();
            placeMarker(lat, lon, 17);
            setInputs(lat, lon);
            say("Pin set to your current position (±" + Math.round(pos.coords.accuracy || 0) + " m).");
            IP.api("/api/geo/reverse?lat=" + lat + "&lon=" + lon)
              .then(function (data) {
                var r = data && data.results && data.results[0];
                if (!r) return;
                fillIfEmpty("city", r.city);
                fillIfEmpty("state", r.state);
                fillIfEmpty("postal_code", r.postcode);
              })
              .catch(function () {});
          },
          function (err) {
            locateBtn.disabled = false;
            say(err && err.code === 1 ? "Location permission denied." : "Couldn't get your location.");
          },
          { enableHighAccuracy: true, timeout: 15000, maximumAge: 30000 }
        );
      });
    }

    whenVisible(container, init);
  });

  /* ------------------------------------------------------------------ read-only map (pin vs. geocoded address) */
  IP.register("map-view", function (container) {
    var data = {};
    try {
      data = JSON.parse(container.getAttribute("data-points") || "{}");
    } catch (e) {
      return;
    }
    whenVisible(container, function () {
      if (!window.L) return leafletMissing(container);
      var map = baseMap(container, { geoapify: container.getAttribute("data-geoapify") === "1" });
      var pts = [];
      if (data.pin) {
        window.L.marker(data.pin, { icon: pinIcon(false), title: data.label || "Pin", keyboard: false }).addTo(map);
        pts.push(data.pin);
      }
      if (data.geocoded) {
        window.L.marker(data.geocoded, { icon: pinIcon(true), title: "Typed address", keyboard: false }).addTo(map);
        pts.push(data.geocoded);
      }
      if (pts.length === 2) {
        window.L.polyline(pts, { color: "#5e5ce6", weight: 2, dashArray: "4 6", opacity: 0.8 }).addTo(map);
        map.fitBounds(pts, { padding: [40, 40], maxZoom: 17 });
      } else if (pts.length === 1) {
        map.setView(pts[0], 16);
      } else {
        map.setView([20, 10], 2);
      }
      setTimeout(function () {
        map.invalidateSize();
      }, 200);
    });
  });
})();

;
/* 30-staff.js */
/* WP-C1 staff & hiring enhancements (progressive: every form works without JavaScript).
 *   data-component="photo-preview"    live preview of the chosen staff photo
 *   data-component="currency-default" payroll country -> default currency (until the user edits it)
 *   data-component="staff-form"       trainer-only hints follow the selected role
 *   data-component="shift-form"       weekday presets + branch field only for offline shifts
 *   data-component="job-form"         "Remote" disables the location field
 */
(function () {
  "use strict";
  var IP = window.IP;
  if (!IP) return;

  // Staff portraits: fall back to the person's initials when an image file is missing.
  document.addEventListener(
    "error",
    function (e) {
      var img = e.target;
      if (!img || img.tagName !== "IMG" || img.hasAttribute("data-preview")) return;
      var box = img.closest && img.closest(".staff-photo[data-initials]");
      if (box) box.textContent = box.getAttribute("data-initials") || "";
    },
    true
  );

  IP.register("photo-preview", function (el) {
    var input = el.querySelector('input[type="file"]');
    var img = el.querySelector("[data-preview]");
    var placeholder = el.querySelector("[data-placeholder]");
    if (!input || !img) return;
    var lastUrl = null;
    input.addEventListener("change", function () {
      var file = input.files && input.files[0];
      if (lastUrl) {
        URL.revokeObjectURL(lastUrl);
        lastUrl = null;
      }
      if (!file || !/^image\//.test(file.type)) return;
      lastUrl = URL.createObjectURL(file);
      img.src = lastUrl;
      img.classList.remove("hidden");
      img.classList.add("size-full", "object-cover");
      if (placeholder) placeholder.classList.add("hidden");
    });
  });

  IP.register("currency-default", function (el) {
    var country = el.querySelector("[data-country]");
    var currency = el.querySelector("[data-currency]");
    if (!country || !currency) return;
    var map = {};
    try {
      map = JSON.parse(el.getAttribute("data-map") || "{}");
    } catch (e) {
      map = {};
    }
    var touched = false;
    currency.addEventListener("input", function () {
      touched = true;
      currency.value = currency.value.toUpperCase().replace(/[^A-Z]/g, "").slice(0, 3);
    });
    country.addEventListener("change", function () {
      var cur = map[country.value];
      if (cur && !touched) {
        currency.value = cur;
        currency.classList.add("ring-2", "ring-accent/40");
        setTimeout(function () {
          currency.classList.remove("ring-2", "ring-accent/40");
        }, 900);
      }
    });
  });

  IP.register("staff-form", function (form) {
    var role = form.querySelector('select[name="role"]');
    var hint = form.querySelector("[data-trainer-hint]");
    if (!role || !hint) return;
    var badge = IP.h("span", { class: "badge badge-violet ml-2 align-middle", text: "Needed for trainer approval" });
    var label = hint.querySelector("label");
    function sync() {
      var trainer = role.value === "trainer";
      if (label) {
        if (trainer && !badge.parentNode) label.appendChild(badge);
        if (!trainer && badge.parentNode) badge.parentNode.removeChild(badge);
      }
    }
    role.addEventListener("change", sync);
    sync();
  });

  IP.register("shift-form", function (form) {
    var boxes = Array.prototype.slice.call(form.querySelectorAll('input[name="weekdays"]'));
    var presets = form.querySelector("[data-weekday-presets]");
    if (presets) {
      presets.classList.remove("hidden");
      presets.classList.add("flex");
      presets.addEventListener("click", function (e) {
        var btn = e.target.closest("[data-preset]");
        if (!btn) return;
        e.preventDefault();
        var wanted = (btn.getAttribute("data-preset") || "").split(",").filter(Boolean);
        boxes.forEach(function (b) {
          b.checked = wanted.indexOf(b.value) !== -1;
        });
      });
    }
    var radios = Array.prototype.slice.call(form.querySelectorAll("[data-mode-radio]"));
    var branchField = form.querySelector("[data-branch-field]");
    function syncMode() {
      if (!branchField) return;
      var selected = radios.filter(function (r) {
        return r.checked;
      })[0];
      var online = selected && selected.value === "online";
      branchField.classList.toggle("hidden", !!online);
      var select = branchField.querySelector("select");
      if (select) select.disabled = !!online;
    }
    radios.forEach(function (r) {
      r.addEventListener("change", syncMode);
    });
    syncMode();
    form.addEventListener("submit", function (e) {
      if (!boxes.some(function (b) { return b.checked; })) {
        e.preventDefault();
        e.stopImmediatePropagation();
        if (IP.island) IP.island.notify({ title: "Pick a weekday", body: "Select at least one day for the shift.", tone: "warning" });
        if (boxes[0]) boxes[0].focus();
      }
    }, true);
  });

  IP.register("job-form", function (form) {
    var remote = form.querySelector("[data-remote-toggle]");
    var field = form.querySelector("[data-location-field]");
    if (!remote || !field) return;
    var input = field.querySelector("input");
    function sync() {
      var on = remote.checked;
      field.classList.toggle("opacity-50", on);
      if (input) {
        input.disabled = on;
        input.placeholder = on ? "Remote" : "City or area";
      }
    }
    remote.addEventListener("change", sync);
    sync();
  });
})();

;
/* 31-finance.js */
/* WP-C2 finance & payroll: chart tooltips, live proof-verification status, proof dropzone.
 * Strict CSP: no inline handlers; DOM is built with IP.h / textContent only.
 */
(function () {
  "use strict";
  var IP = window.IP;
  if (!IP) return;

  /* ------------------------------------------------------------------ 12-month chart tooltip */
  IP.register("fin-chart", function (root) {
    var tip = root.querySelector("[data-tooltip]");
    var frame = tip ? tip.parentElement : null;
    var hits = root.querySelectorAll(".fin-hit");
    if (!tip || !frame || !hits.length) return;
    var active = null;

    function row(keyClass, label, value) {
      return IP.h("div", { class: "mt-1 flex items-center justify-between gap-4" },
        IP.h("span", { class: "flex items-center gap-1.5 opacity-75" },
          keyClass ? IP.h("span", { class: "fin-key " + keyClass, "aria-hidden": "true" }) : null, label),
        IP.h("strong", { class: "tabular-nums", text: value }));
    }

    function show(rect) {
      if (active && active !== rect) active.removeAttribute("data-active");
      active = rect;
      rect.setAttribute("data-active", "1");
      tip.textContent = "";
      tip.appendChild(IP.h("p", { class: "font-semibold", text: rect.getAttribute("data-label") || "" }));
      tip.appendChild(row("fin-key-in", "Income", rect.getAttribute("data-in") || ""));
      tip.appendChild(row("fin-key-out", "Outgoing", rect.getAttribute("data-out") || ""));
      tip.appendChild(row(null, "Net", rect.getAttribute("data-net") || ""));
      var fr = frame.getBoundingClientRect();
      var rr = rect.getBoundingClientRect();
      var x = rr.left - fr.left + rr.width / 2;
      var half = 96;
      x = Math.max(half, Math.min(fr.width - half, x));
      tip.style.left = x + "px";
      tip.style.top = Math.max(0, rr.top - fr.top + 8) + "px";
      tip.hidden = false;
    }

    function hide() {
      tip.hidden = true;
      if (active) active.removeAttribute("data-active");
      active = null;
    }

    hits.forEach(function (rect) {
      // the native <title> is the no-JS fallback; drop it so two tooltips never overlap
      var title = rect.querySelector("title");
      if (title) title.remove();
      rect.addEventListener("pointerenter", function () { show(rect); });
      rect.addEventListener("focus", function () { show(rect); });
      rect.addEventListener("blur", hide);
      rect.addEventListener("keydown", function (e) {
        var list = Array.prototype.slice.call(hits);
        var i = list.indexOf(rect);
        if (e.key === "ArrowRight" && i < list.length - 1) { e.preventDefault(); list[i + 1].focus(); }
        if (e.key === "ArrowLeft" && i > 0) { e.preventDefault(); list[i - 1].focus(); }
        if (e.key === "Escape") { hide(); rect.blur(); }
      });
    });
    root.addEventListener("pointerleave", hide);
  });

  /* ------------------------------------------------------------------ live status while a proof is verified */
  IP.register("payroll-status", function (el) {
    var url = el.getAttribute("data-url");
    var status = el.getAttribute("data-status");
    var explaining = el.getAttribute("data-explaining") === "1";
    if (!url || (status !== "proof_review" && !explaining)) return;
    var tries = 0;
    var stopped = false;

    function reload() {
      if (stopped) return;
      stopped = true;
      window.location.reload();
    }

    async function poll() {
      if (stopped) return;
      tries += 1;
      try {
        var data = await IP.api(url);
        if (data.status !== status || (explaining && !data.explaining)) return reload();
      } catch (err) {
        if (err && (err.status === 401 || err.status === 403 || err.status === 404)) return;
      }
      if (tries < 90) setTimeout(poll, tries < 10 ? 3000 : 6000);
    }

    setTimeout(poll, 2500);
    // a "payment verified / needs attention" notification for this statement refreshes immediately
    var base = url.replace(/\/status$/, "");
    IP.on("notification", function (evt) {
      var link = (evt && evt.link) || "";
      if (link === base || link.indexOf(base + "#") === 0 || link.indexOf(base + "?") === 0) reload();
    });
  });

  /* ------------------------------------------------------------------ "run payroll" for every gym, in the background */
  IP.register("payroll-run", function (el) {
    var id = el.getAttribute("data-task");
    var url = el.getAttribute("data-url");
    var bar = el.querySelector("progress");
    var text = el.querySelector("[data-progress-text]");
    if (!id) return;
    var tries = 0;
    var stopped = false;

    function finish() {
      if (stopped) return;
      stopped = true;
      window.location.reload(); // the statements of every gym (and the outcome) are on the page now
    }

    function show(progress, message) {
      var pct = Number(progress);
      if (bar && isFinite(pct)) bar.value = Math.max(0, Math.min(100, pct));
      if (text && typeof message === "string" && message) {
        text.textContent = message + " - statements appear below as each gym finishes.";
      }
    }

    // the job reports each gym it finishes over the socket ...
    IP.on("task.update", function (evt) {
      if (!evt || String(evt.task_id) !== id) return;
      if (evt.status === "running") show(evt.progress, evt.message);
      else if (evt.status === "done" || evt.status === "failed" || evt.status === "cancelled") finish();
    });

    // ... and a slow poll covers a quiet socket
    async function poll() {
      if (stopped || !url) return;
      tries += 1;
      try {
        var data = await IP.api(url);
        if (data.status === "running" || data.status === "queued" || data.status === "paused") show(data.progress, data.message);
        else return finish();
      } catch (err) {
        if (err && (err.status === 401 || err.status === 403 || err.status === 404)) return;
      }
      if (tries < 120) setTimeout(poll, tries < 10 ? 4000 : 10000);
    }

    setTimeout(poll, 4000);
  });

  /* ------------------------------------------------------------------ proof dropzone */
  IP.register("proof-upload", function (form) {
    var input = form.querySelector('input[type="file"]');
    var zone = form.querySelector("[data-dropzone]");
    var label = form.querySelector("[data-file-label]");
    if (!input || !zone || !label) return;
    var maxMb = parseInt(form.getAttribute("data-max-mb") || "8", 10) || 8;
    var preview = null;

    function human(bytes) {
      if (bytes < 1024 * 1024) return Math.max(1, Math.round(bytes / 1024)) + " KB";
      return (bytes / (1024 * 1024)).toFixed(1) + " MB";
    }

    function describe() {
      var f = input.files && input.files[0];
      if (preview) { URL.revokeObjectURL(preview.src); preview.remove(); preview = null; }
      zone.classList.remove("ring-2", "ring-bad/60");
      if (!f) { label.textContent = "Choose a file"; return; }
      var okType = /^image\//.test(f.type) || f.type === "application/pdf" || /\.(pdf|jpe?g|png|webp|heic)$/i.test(f.name);
      if (!okType) {
        label.textContent = "Please choose an image or a PDF";
        zone.classList.add("ring-2", "ring-bad/60");
        input.value = "";
        return;
      }
      if (f.size > maxMb * 1024 * 1024) {
        label.textContent = f.name + " is larger than " + maxMb + " MB";
        zone.classList.add("ring-2", "ring-bad/60");
        input.value = "";
        return;
      }
      label.textContent = f.name + " · " + human(f.size);
      if (/^image\//.test(f.type)) {
        preview = IP.h("img", { alt: "Selected proof preview", class: "mt-2 max-h-40 rounded-xl object-contain shadow-sm" });
        preview.src = URL.createObjectURL(f);
        zone.appendChild(preview);
      }
    }

    input.addEventListener("change", describe);
    ["dragenter", "dragover"].forEach(function (type) {
      zone.addEventListener(type, function (e) {
        e.preventDefault();
        zone.classList.add("border-accent", "bg-accent/[0.06]");
      });
    });
    ["dragleave", "drop"].forEach(function (type) {
      zone.addEventListener(type, function (e) {
        e.preventDefault();
        zone.classList.remove("border-accent", "bg-accent/[0.06]");
      });
    });
    zone.addEventListener("drop", function (e) {
      if (e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files.length) {
        input.files = e.dataTransfer.files;
        describe();
      }
    });
  });
})();

;
/* 40-checkout.js */
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

;
/* 50-health.js */
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

;
/* 51-coach.js */
/* AI coach page (WP-E2): day tabs for the weekly plan + the progress card of a plan built in the background.
 * Without JavaScript every day is shown stacked, the form posts normally and the progress card is static (reload the
 * page to see the plan).
 */
(function () {
  "use strict";
  var IP = window.IP;
  if (!IP) return;

  var MAX_POLLS = 240; // status checks per page; the WebSocket keeps delivering task.update after that
  var SOCKET_POLL_MS = 10000; // socket up: it delivers task.update, the poll is only a safety net - and a gym's
  // members share one IP (and its rate limit), so a class generating plans at once must not poll every 3 s

  IP.register("coach-days", function (root) {
    var bar = root.querySelector("[data-day-tabs]");
    var tabs = Array.prototype.slice.call(root.querySelectorAll("[data-day-tab]"));
    var days = Array.prototype.slice.call(root.querySelectorAll("[data-coach-day]"));
    if (!bar || days.length < 2) return;
    bar.hidden = false;
    days.forEach(function (d, i) {
      d.setAttribute("role", "tabpanel");
      d.setAttribute("aria-labelledby", "day-tab-" + (i + 1));
      d.removeAttribute("aria-label");
    });

    function select(i, focus) {
      tabs.forEach(function (t, j) {
        var on = j === i;
        t.classList.toggle("tab-active", on);
        t.setAttribute("aria-selected", on ? "true" : "false");
        t.tabIndex = on ? 0 : -1;
        if (on && focus) t.focus();
        if (on && t.scrollIntoView) t.scrollIntoView({ block: "nearest", inline: "nearest" });
      });
      days.forEach(function (d, j) {
        d.hidden = j !== i;
      });
      try {
        sessionStorage.setItem("ip-coach-day", String(i));
      } catch (e) {}
    }

    tabs.forEach(function (t, i) {
      t.addEventListener("click", function () {
        select(i);
      });
    });
    bar.addEventListener("keydown", function (e) {
      var current = tabs.findIndex(function (t) {
        return t.getAttribute("aria-selected") === "true";
      });
      var next = null;
      if (e.key === "ArrowRight") next = (current + 1) % tabs.length;
      else if (e.key === "ArrowLeft") next = (current - 1 + tabs.length) % tabs.length;
      else if (e.key === "Home") next = 0;
      else if (e.key === "End") next = tabs.length - 1;
      if (next !== null) {
        e.preventDefault();
        select(next, true);
      }
    });

    var start = 0;
    var m = /^#day-(\d+)$/.exec(location.hash || "");
    if (m) start = parseInt(m[1], 10) - 1;
    else {
      try {
        start = parseInt(sessionStorage.getItem("ip-coach-day") || "0", 10) || 0;
      } catch (e) {}
    }
    select(Math.max(0, Math.min(start, days.length - 1)));
  });

  /* A plan being built in the background (the coach_plan job of POST /me/coach/generate). The card is rendered while
   * the job is queued, running or paused; it follows the job over the WebSocket (task.update, kind coach_plan) and
   * polls GET /me/coach/status as a fallback: every 3-5 s while the socket is down, every 10 s while it is up. Done:
   * the page reloads to show the saved plan; failed or stopped: it reloads to the notice. Never starts, retries or
   * cancels anything itself. */
  IP.register("coach-job", function (card) {
    var jobId = card.getAttribute("data-job") || "";
    var url = card.getAttribute("data-status-url");
    var title = card.querySelector("[data-coach-job-title]");
    var text = card.querySelector("[data-coach-job-text]");
    if (!jobId || !url) return;
    var TEXT = {
      queued: ["Building your plan", "Queued - it starts in a moment."],
      running: ["Building your plan", "Choosing exercises for your goal and running the safety checks…"],
      paused: ["Plan paused", "Paused while you were away - resumes now that you're back."],
    };
    var FINAL = { done: true, failed: true, cancelled: true };
    var stopped = false;
    var tries = 0;
    var timer = null;

    function show(status) {
      var t = TEXT[status];
      if (!t) return;
      card.setAttribute("data-status", status);
      if (title) title.textContent = t[0];
      if (text) text.textContent = t[1];
    }

    function finish(status) {
      if (stopped) return;
      stopped = true;
      if (timer) clearTimeout(timer);
      // done: the plan is saved - show it; failed / stopped: ?generating= keeps the page's notice about this request
      window.location.replace(status === "done" ? "/me/coach" : "/me/coach?generating=" + encodeURIComponent(jobId));
    }

    function handle(status) {
      if (FINAL[status]) finish(status);
      else show(status);
    }

    IP.on("task.update", function (evt) {
      if (stopped || !evt || evt.kind !== "coach_plan" || String(evt.task_id) !== jobId) return;
      handle(String(evt.status || ""));
    });

    function schedule(ms) {
      if (timer) clearTimeout(timer);
      timer = setTimeout(poll, ms);
    }

    async function poll() {
      timer = null;
      if (stopped) return;
      if (document.hidden) return schedule(5000); // nobody is looking: ask again later (not counted)
      tries += 1;
      try {
        var data = await IP.api(url);
        if (data && data.status) handle(String(data.status));
      } catch (err) {
        if (err && (err.status === 401 || err.status === 403 || err.status === 404)) {
          stopped = true; // signed out, or no longer this member's request: stop asking
          return;
        }
      }
      if (!stopped && tries < MAX_POLLS) schedule(IP.ws && IP.ws.connected ? SOCKET_POLL_MS : tries < 10 ? 3000 : 5000);
    }

    IP.on("page.restored", function () {
      if (!stopped) schedule(0); // back from the back/forward cache: catch up at once
    });
    schedule(3000);
  });
})();

;
/* 52-assistant.js */
/* AI assistant (WP-E2): FAB -> bottom sheet on phones, floating panel on larger screens.
 * Chat bubbles, typing dots, action result chips with links, suggestion chips, live AI status,
 * Esc to close, focus trap, Cmd/Ctrl+K shortcut. Server contract: see partials/assistant_sheet.html.
 *
 * AI requests go through IP.ai (docs/ai-guard-and-queue.md section 7):
 * - a refusal (quota, cooldown, too many at once, duplicate, spam) shows the server's own explanation; the message
 *   was not sent, so its words go back into the input. While IP.ai.cooldownLeft("assistant") > 0 the send button
 *   is disabled and a small line counts the wait down (IP.ai.onCooldown says when a wait starts and ends).
 * - closing the sheet aborts the request in flight, so the server stops the AI work nobody would read. The words go
 *   back into the input and the next open reloads the history (the server is the source of truth).
 * - leaving the page aborts it too (IP.ai, reason "page-hide"): not an error, never reported.
 * - a full AI queue is answered with basic commands ("Basic mode · AI busy"); AI itself is still online.
 * Strict CSP: DOM is built with IP.h / textContent only.
 */
(function () {
  "use strict";
  var IP = window.IP;
  if (!IP || !IP.ai) return; // 01-ai.js is bundled before this file

  var FEATURE = "assistant"; // the server's AI policy name: IP.ai keeps this feature's cooldown
  var CHAT_URL = "/api/assistant/chat";
  // refusals the server words precisely ("You've used your AI chat messages for this minute ...")
  var SERVER_WORDS = { quota: true, cooldown: true, too_many: true, duplicate: true, spam: true };
  // answers that guarantee the message was neither executed nor stored: its words go back into the input
  var UNSENT = { quota: true, cooldown: true, too_many: true, duplicate: true, spam: true, busy: true,
                 cooldown_local: true, cancelled: true };
  var META = "px-1 text-[11px] text-zinc-500 dark:text-zinc-400";
  var NS = "http://www.w3.org/2000/svg";
  var PATHS = {
    check: [["polyline", { points: "20 6 9 17 4 12" }]],
    x: [["line", { x1: "18", y1: "6", x2: "6", y2: "18" }], ["line", { x1: "6", y1: "6", x2: "18", y2: "18" }]],
    arrow: [["line", { x1: "5", y1: "12", x2: "19", y2: "12" }], ["polyline", { points: "12 5 19 12 12 19" }]],
  };
  var PROVIDERS = { gateway: "Gemma (gateway)", docker: "Gemma (Docker failsafe)", anthropic: "Claude (Anthropic)" };

  function svg(name, cls) {
    var s = document.createElementNS(NS, "svg");
    s.setAttribute("viewBox", "0 0 24 24");
    s.setAttribute("fill", "none");
    s.setAttribute("stroke", "currentColor");
    s.setAttribute("stroke-width", "2");
    s.setAttribute("stroke-linecap", "round");
    s.setAttribute("stroke-linejoin", "round");
    s.setAttribute("aria-hidden", "true");
    s.setAttribute("class", cls || "size-3.5 shrink-0");
    (PATHS[name] || []).forEach(function (p) {
      var el = document.createElementNS(NS, p[0]);
      Object.keys(p[1]).forEach(function (k) {
        el.setAttribute(k, p[1][k]);
      });
      s.appendChild(el);
    });
    return s;
  }

  /** What to say about a failed send: the server's own words for a guard / queue refusal, IP.ai's otherwise. */
  function errorText(err) {
    var code = err && typeof err.code === "string" ? err.code : "";
    var data = err && err.data && typeof err.data === "object" ? err.data : null;
    var detail = data && typeof data.detail === "string" ? data.detail.trim() : "";
    if (SERVER_WORDS[code] && detail && detail.length <= 300) return detail;
    return IP.ai.describeError(err);
  }

  /** Stopped on purpose: "caller" = we aborted it (sheet closed), "page-hide" = IP.ai did (the page is going away). */
  function isQuietAbort(err) {
    return !!err && err.name === "AbortError" && (err.reason === "caller" || err.reason === "page-hide");
  }

  IP.register("assistant", function (sheet) {
    var log = sheet.querySelector("[data-assistant-log]");
    var form = sheet.querySelector("[data-assistant-form]");
    var input = sheet.querySelector("[data-assistant-input]") || (form && form.querySelector('[name="message"]'));
    var sendBtn = sheet.querySelector("[data-assistant-send]");
    var statusEl = sheet.querySelector("[data-assistant-status]");
    var dotEl = sheet.querySelector("[data-assistant-dot]");
    var cooldownEl = sheet.querySelector("[data-assistant-cooldown]");
    var intro = sheet.querySelector("[data-assistant-intro]");
    var backdrop = document.querySelector("[data-assistant-backdrop]");
    var small = window.matchMedia ? window.matchMedia("(max-width: 639px)") : null;
    if (!log || !form || !input) return;

    var lastFocus = null;
    var historyPromise = null;
    var busy = false;
    var inflight = null; // AbortController of the message being answered
    var resync = false; // a request was aborted - the server may have answered it after all: reload history on open
    var paused = false; // the running wait is an account-wide AI pause (code "cooldown")
    var tick = null;

    function isOpen() {
      return sheet.getAttribute("data-open") === "1";
    }

    function setExpanded(v) {
      document.querySelectorAll("[data-assistant-open]").forEach(function (b) {
        b.setAttribute("aria-expanded", v ? "true" : "false");
      });
    }

    function lockScroll(lock) {
      if (small && small.matches) document.body.classList.toggle("overflow-hidden", lock);
      else document.body.classList.remove("overflow-hidden");
    }

    function scrollDown() {
      log.scrollTop = log.scrollHeight;
    }

    function autoresize() {
      input.style.height = "auto";
      input.style.height = Math.min(input.scrollHeight, 128) + "px";
    }

    function setStatus(available, label) {
      if (statusEl) statusEl.textContent = available ? label || "AI online" : "AI unavailable - basic commands still work";
      if (dotEl) dotEl.setAttribute("data-state", available ? "online" : "offline");
      sheet.setAttribute("data-ai-available", available ? "1" : "0");
    }

    function hideIntro() {
      if (intro && intro.parentNode) intro.parentNode.removeChild(intro);
      intro = null;
    }

    function addMessage(role, text, actions, meta) {
      hideIntro();
      var mine = role === "user";
      var wrap = IP.h("div", { class: "flex flex-col gap-1.5 animate-fade-up " + (mine ? "items-end" : "items-start") });
      wrap.appendChild(IP.h("div", { class: "bubble " + (mine ? "bubble-user" : "bubble-ai"), text: text || "" }));
      if (actions && actions.length) {
        var row = IP.h("div", { class: "flex max-w-[92%] flex-wrap gap-1.5" });
        actions.forEach(function (a) {
          var chip = IP.h(a.link ? "a" : "span", { class: "action-chip", "data-ok": a.ok ? "1" : "0", href: a.link || null });
          chip.appendChild(svg(a.ok ? "check" : "x"));
          chip.appendChild(IP.h("span", { class: "min-w-0 break-words", text: a.detail || a.type }));
          if (a.link) chip.appendChild(svg("arrow", "size-3 shrink-0 opacity-60"));
          row.appendChild(chip);
        });
        wrap.appendChild(row);
      }
      if (meta) wrap.appendChild(IP.h("span", { class: META, text: meta }));
      log.appendChild(wrap);
      scrollDown();
      return wrap;
    }

    function typing() {
      var dots = IP.h("span", { class: "typing-dots" }, IP.h("span"), IP.h("span"), IP.h("span"));
      var row = IP.h("div", { class: "flex items-start", role: "status", "aria-label": "Assistant is typing" },
        IP.h("div", { class: "bubble bubble-ai" }, dots));
      log.appendChild(row);
      scrollDown();
      return row;
    }

    function clearLog() {
      while (log.firstChild) log.removeChild(log.firstChild);
      intro = null;
    }

    /** Resolves with the history items, or null when they could not be loaded (never rejects). `replace` swaps the
     * whole log for the server's history - after an aborted request. */
    function loadHistory(replace) {
      if (historyPromise && !replace) return historyPromise;
      var p = IP.api("/api/assistant/history").then(
        function (data) {
          var items = (data && data.items) || [];
          if (p !== historyPromise) return items; // a newer load owns the log
          if (replace) clearLog();
          items.forEach(function (m) {
            addMessage(m.role === "user" ? "user" : "assistant", m.content, m.role === "user" ? null : m.actions);
          });
          return items;
        },
        function () {
          if (p === historyPromise) historyPromise = null;
          return null;
        }
      );
      historyPromise = p;
      return p;
    }

    function resyncHistory() {
      if (!resync || busy) return;
      resync = false;
      loadHistory(true).then(function (items) {
        if (!items) resync = true; // try again on the next open
      });
    }

    /* ------------------------------------------------------------- the server asked us to wait */
    function waitLeft() {
      return IP.ai.cooldownLeft(FEATURE);
    }

    function updateSend() {
      if (!sendBtn) return;
      sendBtn.disabled = busy || waitLeft() > 0;
      sendBtn.setAttribute("aria-busy", busy ? "true" : "false");
    }

    function paintCooldown() {
      var left = waitLeft();
      if (cooldownEl) {
        cooldownEl.textContent = left > 0
          ? (paused ? "AI is paused for your account. " : "") + "You can send again in " + IP.ai.formatWait(left) + "."
          : "";
        cooldownEl.hidden = !(left > 0);
      }
      if (left > 0 && !tick) tick = setInterval(paintCooldown, 1000);
      if (!(left > 0)) {
        paused = false;
        if (tick) {
          clearInterval(tick);
          tick = null;
        }
      }
      updateSend();
    }

    IP.ai.onCooldown(function (info) {
      if (info && !info.ended && (info.feature === FEATURE || info.code === "cooldown")) paused = info.code === "cooldown";
      paintCooldown();
    });

    /* ------------------------------------------------------------- open / close */
    function open(prefill) {
      if (!isOpen()) {
        lastFocus = document.activeElement;
        sheet.removeAttribute("inert");
        sheet.setAttribute("aria-hidden", "false");
        sheet.setAttribute("data-open", "1");
        if (backdrop) {
          backdrop.hidden = false;
          requestAnimationFrame(function () {
            backdrop.setAttribute("data-open", "1");
          });
        }
        setExpanded(true);
        lockScroll(true);
        if (resync) resyncHistory();
        else loadHistory();
        paintCooldown();
      }
      if (prefill) {
        input.value = prefill;
        autoresize();
      }
      setTimeout(function () {
        input.focus();
        if (prefill) input.setSelectionRange(input.value.length, input.value.length);
      }, 80);
    }

    function close() {
      if (!isOpen()) return;
      if (inflight) inflight.abort(); // nobody will read the answer: the server stops the AI work
      sheet.setAttribute("data-open", "0");
      sheet.setAttribute("aria-hidden", "true");
      sheet.setAttribute("inert", "");
      if (backdrop) {
        backdrop.setAttribute("data-open", "0");
        setTimeout(function () {
          if (!isOpen()) backdrop.hidden = true;
        }, 320);
      }
      setExpanded(false);
      lockScroll(false);
      if (lastFocus && typeof lastFocus.focus === "function" && document.contains(lastFocus)) lastFocus.focus();
    }

    function setBusy(v) {
      busy = v;
      updateSend();
    }

    /** The message did not go through: hand its words back, unless something new was typed meanwhile. */
    function giveBack(text) {
      if (input.value.trim()) return;
      input.value = text;
      autoresize();
    }

    function showAnswer(res) {
      var meta = null;
      if (res.busy) meta = "Basic mode · AI busy";
      else if (!res.ai_available) meta = "Basic mode · AI offline";
      var bubble = addMessage("assistant", res.reply, res.actions, meta);
      // "Remember this?" / "Forgotten - Undo" cards (54-memory.js): nothing is saved until the member confirms
      if (IP.memory && res.memory && res.memory.length) {
        IP.memory.render(bubble, res.memory);
        scrollDown();
      }
      // a full AI queue says nothing about AI being offline: leave the status line alone
      if (!res.busy) setStatus(!!res.ai_available, res.provider ? PROVIDERS[res.provider] || res.provider : null);
      var changed = (res.actions || []).some(function (a) {
        return a.ok && /task|reminder/.test(a.type);
      });
      if (changed) IP.emit("tasks.changed", { actions: res.actions });
    }

    async function send(text) {
      text = (text || "").trim();
      if (!text || busy) return;
      if (waitLeft() > 0) {
        // the send button is disabled - Enter and the suggestion chips end up here: keep the words, show the wait
        giveBack(text);
        paintCooldown();
        return;
      }
      if (text.length > 1000) text = text.slice(0, 1000);
      setBusy(true);
      await loadHistory();
      if (!isOpen()) {
        // closed while the history was loading: nothing was asked yet, and nobody would read the answer
        setBusy(false);
        giveBack(text);
        return;
      }
      var mine = addMessage("user", text);
      input.value = "";
      autoresize();
      var dots = typing();
      var ctrl = typeof AbortController === "function" ? new AbortController() : null;
      inflight = ctrl;
      try {
        var res = await IP.ai.request(CHAT_URL, { json: { message: text }, feature: FEATURE, signal: ctrl ? ctrl.signal : undefined });
        dots.remove();
        showAnswer(res || {});
      } catch (err) {
        dots.remove();
        if (isQuietAbort(err)) {
          // stopped on purpose: no error bubble. The server dropped the work - unless it had just finished - so the
          // next open shows the real history; the words wait in the input.
          resync = true;
          giveBack(text);
        } else {
          if (err && UNSENT[err.code]) {
            mine.appendChild(IP.h("span", { class: META, text: "Not sent" }));
            giveBack(text);
          }
          addMessage("assistant", errorText(err));
        }
      } finally {
        if (inflight === ctrl) inflight = null;
        setBusy(false);
        paintCooldown();
        if (isOpen()) input.focus();
      }
    }

    // ------------------------------------------------------------- wiring
    document.addEventListener("click", function (e) {
      var opener = e.target.closest && e.target.closest("[data-assistant-open]");
      if (opener) {
        e.preventDefault();
        if (isOpen() && opener.classList.contains("fab")) close();
        else open(opener.getAttribute("data-assistant-prefill"));
        return;
      }
      if (e.target.closest && e.target.closest("[data-assistant-close]")) close();
    });
    if (backdrop) backdrop.addEventListener("click", close);

    sheet.addEventListener("click", function (e) {
      var s = e.target.closest && e.target.closest("[data-assistant-suggest]");
      if (!s) return;
      var text = s.getAttribute("data-assistant-suggest") || "";
      if (/^message my /i.test(text)) {
        input.value = text + ": ";
        autoresize();
        input.focus();
        input.setSelectionRange(input.value.length, input.value.length);
      } else {
        send(text);
      }
    });

    form.addEventListener("submit", function (e) {
      e.preventDefault();
      send(input.value);
    });

    input.addEventListener("input", autoresize);
    input.addEventListener("keydown", function (e) {
      if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
        e.preventDefault();
        send(input.value);
      }
    });

    // focus trap + Esc
    sheet.addEventListener("keydown", function (e) {
      if (e.key === "Escape") {
        e.preventDefault();
        close();
        return;
      }
      if (e.key !== "Tab") return;
      var focusable = Array.prototype.filter.call(
        sheet.querySelectorAll('a[href], button:not([disabled]), textarea:not([disabled]), input:not([disabled]), [tabindex]:not([tabindex="-1"])'),
        function (el) {
          return el.offsetParent !== null;
        }
      );
      if (!focusable.length) return;
      var first = focusable[0];
      var last = focusable[focusable.length - 1];
      if (e.shiftKey && document.activeElement === first) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && document.activeElement === last) {
        e.preventDefault();
        first.focus();
      }
    });
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape" && isOpen()) close();
      if ((e.metaKey || e.ctrlKey) && (e.key === "k" || e.key === "K")) {
        e.preventDefault();
        if (isOpen()) close();
        else open();
      }
    });
    if (small && small.addEventListener) {
      small.addEventListener("change", function () {
        lockScroll(isOpen());
      });
    }

    // live AI status from the WebSocket
    IP.on("ai.status", function (evt) {
      if (evt) setStatus(!!evt.available, evt.active);
    });
    IP.on("hello", function (evt) {
      if (evt && evt.ai) setStatus(evt.ai.state !== "offline", evt.ai.active);
    });
    // back from the back/forward cache with the sheet still open after a page-hide abort: show the real history
    IP.on("page.restored", function () {
      if (isOpen()) resyncHistory();
    });

    paintCooldown();
    IP.assistant = { open: open, close: close, send: send };
  });
})();

;
/* 53-calendar.js */
/* Calendar & tasks (WP-E2): day selection without page reloads, day panel rendering, keyboard navigation,
 * AJAX task toggles (progressive enhancement over plain POST forms) and reminder presets on the task form.
 */
(function () {
  "use strict";
  var IP = window.IP;
  if (!IP) return;

  var LABELS = { task: "To-do", reminder: "Reminder", event: "Event", workout: "Workout", bedtime: "Bedtime", renewal: "Renewal", shift: "Shift" };
  var NS = "http://www.w3.org/2000/svg";

  function checkIcon() {
    var s = document.createElementNS(NS, "svg");
    s.setAttribute("viewBox", "0 0 24 24");
    s.setAttribute("fill", "none");
    s.setAttribute("stroke", "currentColor");
    s.setAttribute("stroke-width", "2.2");
    s.setAttribute("stroke-linecap", "round");
    s.setAttribute("stroke-linejoin", "round");
    s.setAttribute("class", "size-3.5");
    s.setAttribute("aria-hidden", "true");
    var p = document.createElementNS(NS, "polyline");
    p.setAttribute("points", "20 6 9 17 4 12");
    s.appendChild(p);
    return s;
  }

  function parseIso(iso) {
    var p = iso.split("-");
    return new Date(+p[0], +p[1] - 1, +p[2]);
  }

  function pad(n) {
    return (n < 10 ? "0" : "") + n;
  }

  async function toggleTask(id) {
    return IP.api("/tasks/" + encodeURIComponent(id) + "/toggle", { method: "POST" });
  }

  IP.register("calendar", function (root) {
    var days = {};
    try {
      days = JSON.parse(root.getAttribute("data-days") || "{}") || {};
    } catch (e) {
      days = {};
    }
    var month = root.getAttribute("data-month");
    var today = root.getAttribute("data-today");
    var panel = root.querySelector("[data-day-panel]");
    if (!panel) return;
    var list = panel.querySelector("[data-day-items]");
    var titleEl = panel.querySelector("[data-day-title]");
    var eyebrowEl = panel.querySelector("[data-day-eyebrow]");
    var quickDay = root.querySelector("[data-quick-add-day]");
    var dueInput = root.querySelector("[data-due-date]");
    var nextInput = root.querySelector('form[action="/tasks"] input[name="next"]');
    var cells = Array.prototype.slice.call(root.querySelectorAll("[data-day]"));
    var small = window.matchMedia ? window.matchMedia("(max-width: 1023px)") : null;

    function itemRow(it, iso) {
      var li = IP.h("li", { class: "flex items-start gap-3 rounded-2xl bg-zinc-900/[0.03] p-3 dark:bg-white/[0.04]" });
      li.appendChild(IP.h("span", { class: "mt-1.5 size-2.5 shrink-0 rounded-full dot-" + (it.tone || "blue") }));
      var meta = (it.time || "All day") + " · " + (LABELS[it.kind] || it.label || it.kind) + (it.detail ? " · " + it.detail : "");
      li.appendChild(
        IP.h("div", { class: "min-w-0 flex-1" },
          IP.h("p", { class: "font-medium break-words" + (it.done ? " line-through opacity-60" : ""), text: it.title }),
          IP.h("p", { class: "mt-0.5 text-xs text-zinc-500 dark:text-zinc-400", text: meta }))
      );
      if (it.task_id) {
        var btn = IP.h("button", {
          type: "button", class: "task-check", "aria-pressed": it.done ? "true" : "false",
          "aria-label": (it.done ? "Mark as not done: " : "Mark as done: ") + it.title,
        });
        btn.appendChild(checkIcon());
        btn.addEventListener("click", async function () {
          btn.disabled = true;
          try {
            var res = await toggleTask(it.task_id);
            it.done = !!res.completed;
            render(iso);
            syncCell(iso);
            if (IP.island) IP.island.notify({ title: it.done ? "Task completed" : "Task reopened", tone: it.done ? "success" : "info", timeout: 1600 });
          } catch (err) {
            if (IP.island) IP.island.notify({ title: "Could not update the task", body: err.message, tone: "danger" });
            btn.disabled = false;
          }
        });
        li.appendChild(btn);
      } else if (it.link) {
        var a = IP.h("a", { href: it.link, class: "btn btn-ghost btn-sm shrink-0", text: "Open" });
        li.appendChild(a);
      }
      return li;
    }

    function syncCell(iso) {
      var cell = root.querySelector('[data-day="' + iso + '"]');
      if (!cell) return;
      var pills = cell.querySelectorAll(".cal-pill");
      (days[iso] || []).slice(0, pills.length).forEach(function (it, i) {
        pills[i].setAttribute("data-done", it.done ? "1" : "0");
      });
    }

    function render(iso) {
      var items = days[iso] || [];
      var d = parseIso(iso);
      eyebrowEl.textContent = iso === today ? "Today" : d.toLocaleDateString(undefined, { weekday: "long" });
      titleEl.textContent = d.toLocaleDateString(undefined, { day: "2-digit", month: "long", year: "numeric" });
      list.textContent = "";
      if (!items.length) {
        list.appendChild(IP.h("li", {
          class: "rounded-2xl border border-dashed border-black/10 px-4 py-6 text-center text-sm text-zinc-500 dark:border-white/10 dark:text-zinc-400",
          text: "Nothing planned for this day.",
        }));
      }
      items.forEach(function (it) {
        list.appendChild(itemRow(it, iso));
      });
    }

    function select(iso, opts) {
      opts = opts || {};
      cells.forEach(function (c) {
        c.setAttribute("data-selected", c.getAttribute("data-day") === iso ? "1" : "0");
      });
      render(iso);
      if (quickDay) quickDay.textContent = "For " + parseIso(iso).toLocaleDateString(undefined, { weekday: "short", day: "2-digit", month: "short" });
      if (dueInput) {
        dueInput.value = iso;
        dueInput.dispatchEvent(new Event("input", { bubbles: true }));
      }
      var url = "/calendar?month=" + month + "&day=" + iso;
      if (nextInput) nextInput.value = url;
      try {
        history.replaceState(null, "", url);
      } catch (e) {}
      if (opts.scroll && small && small.matches && panel.scrollIntoView) panel.scrollIntoView({ behavior: "smooth", block: "start" });
    }

    cells.forEach(function (cell, idx) {
      cell.addEventListener("click", function (e) {
        if (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
        var iso = cell.getAttribute("data-day");
        if (iso.slice(0, 7) !== month) return; // other month: navigate normally
        e.preventDefault();
        select(iso, { scroll: true });
      });
      cell.addEventListener("keydown", function (e) {
        var step = { ArrowLeft: -1, ArrowRight: 1, ArrowUp: -7, ArrowDown: 7 }[e.key];
        if (!step) return;
        var target = cells[idx + step];
        if (target) {
          e.preventDefault();
          target.focus();
        }
      });
    });

    IP.on("task.toggled", function (evt) {
      Object.keys(days).forEach(function (iso) {
        (days[iso] || []).forEach(function (it) {
          if (String(it.task_id) === String(evt.id)) {
            it.done = !!evt.completed;
            syncCell(iso);
          }
        });
      });
    });
    IP.on("tasks.changed", function () {
      // the assistant created or changed tasks: the month view refreshes on the next visit
      root.setAttribute("data-stale", "1");
    });
  });

  // progressive enhancement for the toggle buttons on the tasks page
  document.addEventListener("submit", async function (e) {
    var form = e.target;
    if (!form.matches || !form.matches("form[data-task-toggle]")) return;
    e.preventDefault();
    var btn = form.querySelector(".task-check");
    var id = (form.getAttribute("action") || "").split("/")[2];
    try {
      var res = await toggleTask(id);
      if (btn) {
        btn.setAttribute("aria-pressed", res.completed ? "true" : "false");
        btn.setAttribute("aria-label", (res.completed ? "Mark as not done: " : "Mark as done: ") + (btn.getAttribute("aria-label") || "").replace(/^Mark as (not )?done: /, ""));
      }
      var row = form.closest("li");
      var title = row && (row.querySelector("[data-task-title]") || row.querySelector("p"));
      if (title) {
        title.classList.toggle("line-through", !!res.completed);
        title.classList.toggle("text-zinc-400", !!res.completed);
      }
      IP.emit("task.toggled", { id: id, completed: !!res.completed });
      if (IP.island) IP.island.notify({ title: res.completed ? "Task completed" : "Task reopened", tone: res.completed ? "success" : "info", timeout: 1600 });
    } catch (err) {
      form.submit();
    }
  });

  IP.register("task-form", function (form) {
    var presets = form.querySelector("[data-remind-presets]");
    var dateEl = form.querySelector('[name="due_date"]');
    var timeEl = form.querySelector('[name="due_time"]');
    var remindEl = form.querySelector('[name="remind_at"]');
    if (!presets || !dateEl || !timeEl || !remindEl) return;
    function update() {
      presets.hidden = !(dateEl.value && timeEl.value);
    }
    dateEl.addEventListener("input", update);
    dateEl.addEventListener("change", update);
    timeEl.addEventListener("input", update);
    timeEl.addEventListener("change", update);
    update();
    presets.addEventListener("click", function (e) {
      var b = e.target.closest("[data-remind-offset]");
      if (!b || !dateEl.value || !timeEl.value) return;
      var p = dateEl.value.split("-");
      var t = timeEl.value.split(":");
      var d = new Date(+p[0], +p[1] - 1, +p[2], +t[0], +t[1]);
      d = new Date(d.getTime() - (+b.getAttribute("data-remind-offset") || 0) * 60000);
      remindEl.value = d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate()) + "T" + pad(d.getHours()) + ":" + pad(d.getMinutes());
      presets.querySelectorAll("[data-remind-offset]").forEach(function (x) {
        x.classList.toggle("!bg-accent", x === b);
        x.classList.toggle("!text-white", x === b);
      });
    });
  });
})();

;
/* 54-memory.js */
/* "Remember this?" cards (app/ai/memory.py, docs/ai-guard-and-queue.md section 13) for the assistant sheet and the
 * live coach: IP.memory.render(container, entries) draws one card per `memory` entry of an answer.
 *  - status "proposed": Remember / Not now. Remember posts the signed ticket to /api/memory/confirm (once - the
 *    buttons are disabled while it is in flight, so a double click never sends it twice), then offers Undo.
 *  - status "known" / "full" / op "forgot": the server's words; a forget comes with Undo (/api/memory/undo).
 * Nothing is stored until the member presses Remember. Strict CSP: DOM built with IP.h / textContent only.
 */
(function () {
  "use strict";
  var IP = window.IP;
  if (!IP) return;

  var CARD = "mt-1.5 flex w-full max-w-[92%] flex-col gap-1.5 rounded-2xl bg-accent/[0.06] p-3 text-sm ring-1 ring-accent/15 dark:bg-white/[0.05]";
  var BTN = "btn btn-sm";
  var PAGE = "/me/health/profile/ai";

  function words(err) {
    var data = err && err.data && typeof err.data === "object" ? err.data : null;
    var detail = data && typeof data.detail === "string" ? data.detail : "";
    if (detail && detail.length <= 400) return detail;
    if (err && err.status === 429) return "Too many requests - try again in a moment.";
    return "That didn't go through - please try again.";
  }

  function line(text, cls) {
    return IP.h("p", { class: cls || "text-xs text-zinc-600 dark:text-zinc-300", text: text || "" });
  }

  function buttons() {
    return IP.h("div", { class: "flex flex-wrap items-center gap-2" });
  }

  /** Swap the card's actions for a status line (+ an optional Undo with its own ticket). */
  function settle(card, actions, text, undo, onUndone) {
    while (actions.firstChild) actions.removeChild(actions.firstChild);
    actions.appendChild(line(text, "text-xs font-medium text-zinc-700 dark:text-zinc-200"));
    if (undo) {
      var u = IP.h("button", { type: "button", class: BTN + " btn-ghost", text: "Undo" });
      u.addEventListener("click", function () {
        if (u.disabled) return;
        u.disabled = true;
        IP.api("/api/memory/undo", { method: "POST", json: { token: undo } }).then(
          function (res) {
            settle(card, actions, (res && res.detail) || "Undone.", null);
            if (onUndone) onUndone(res);
          },
          function (err) {
            u.disabled = false;
            actions.appendChild(line(words(err), "text-xs text-bad"));
          }
        );
      });
      actions.appendChild(u);
    }
    var link = IP.h("a", { href: PAGE, class: "text-xs font-medium text-accent hover:underline", text: "What the AI knows" });
    actions.appendChild(link);
  }

  function card(entry, opts) {
    var box = IP.h("div", { class: CARD, role: "group", "aria-label": "Memory" });
    var title = entry.op === "forgot" ? "Forgotten" : entry.status === "proposed" ? "Remember this?" : "Your profile";
    box.appendChild(line(title, "text-[11px] font-semibold uppercase tracking-wide text-accent"));
    if (entry.label) box.appendChild(IP.h("p", { class: "font-medium [overflow-wrap:anywhere]", text: entry.label }));
    if (entry.detail) box.appendChild(line(entry.detail));
    var actions = buttons();
    box.appendChild(actions);
    if (entry.op === "forgot") {
      settle(box, actions, entry.status === "done" ? "Removed everywhere." : "Nothing removed.", entry.undo || null, opts.onChange);
      return box;
    }
    if (entry.status !== "proposed" || !entry.token) {
      settle(box, actions, entry.status === "known" ? "Already saved." : "Not saved.", null);
      return box;
    }
    var yes = IP.h("button", { type: "button", class: BTN + " btn-primary", text: "Remember" });
    var no = IP.h("button", { type: "button", class: BTN + " btn-ghost", text: "Not now" });
    yes.addEventListener("click", function () {
      if (yes.disabled) return; // one ticket, one request: double clicks are ignored
      yes.disabled = no.disabled = true;
      yes.setAttribute("aria-busy", "true");
      IP.api("/api/memory/confirm", { method: "POST", json: { token: entry.token } }).then(
        function (res) {
          settle(box, actions, (res && res.detail) || "Saved.", res && res.undo, opts.onChange);
          if (opts.onChange) opts.onChange(res);
        },
        function (err) {
          yes.removeAttribute("aria-busy");
          var expired = err && err.data && (err.data.code === "expired" || err.data.code === "full" || err.data.code === "invalid");
          settle(box, actions, words(err), null);
          if (!expired) {
            yes.disabled = no.disabled = false;
            actions.insertBefore(no, actions.firstChild);
            actions.insertBefore(yes, actions.firstChild);
          }
        }
      );
    });
    no.addEventListener("click", function () {
      if (no.disabled) return;
      settle(box, actions, "Not saved.", null);
    });
    actions.appendChild(yes);
    actions.appendChild(no);
    return box;
  }

  /** Draw the `memory` entries of an answer into `container` (returns the number drawn). */
  function render(container, entries, opts) {
    if (!container || !entries || !entries.length) return 0;
    opts = opts || {};
    var n = 0;
    entries.slice(0, 5).forEach(function (e) {
      if (!e || typeof e !== "object") return;
      container.appendChild(card(e, opts));
      n += 1;
    });
    return n;
  }

  IP.memory = { render: render };
})();

;
/* 55-live-coach.js */
/* Live AI workout companion (WP-K): start screen + live session runtime.
 *   data-component="live-start"    workout picker, voice/mic preferences -> POST /api/live/sessions
 *   data-component="live-session"  coach stage (IP.coachFace), workout HUD, conversation, discomfort flow, safety screen
 * Voice out: POST /api/tts (Kokoro) -> <audio> + Web Audio analyser for lip-sync; 503/429/offline -> speechSynthesis
 *            with a soft voice and boundary/procedural lip-sync, and no new voice requests until Retry-After (or the
 *            page-wide IP.ai "tts" cooldown) has passed. Audio starts only after the member taps Begin.
 * Voice in:  (webkit)SpeechRecognition - continuous + interim results, auto-restart, barge-in, push-to-talk.
 *            The text box always works. All text goes into the DOM with textContent (never innerHTML).
 * AI chat:   one message at a time through IP.ai (01-ai.js) - a newer message replaces the one in flight, a voice
 *            repeat of it is dropped. A refused reply (HTTP 200 + `refused`) is spoken once and handed to
 *            IP.ai.noteRefusal, like a 429: AI chat waits `retry_after` seconds (IP.ai's "live_coach" cooldown is the
 *            only timer), and code "cooldown" pauses every AI widget of the page and the natural voice too. Meanwhile
 *            messages carry "ai": false (the server still runs every safety check and command, without the AI).
 *            Discomfort reports are safety flows: never refused or aborted locally.
 *            Leaving the page aborts what is in flight and pauses the workout (keepalive event) - not on sign-out.
 */
(function () {
  "use strict";
  var IP = window.IP;
  if (!IP) return;

  var SR = window.SpeechRecognition || window.webkitSpeechRecognition || null;
  var PREFS_KEY = "ip-live-prefs";
  var SOFT_VOICES = ["Samantha", "Ava", "Allison", "Karen", "Moira", "Tessa", "Google UK English Female",
    "Microsoft Aria Online (Natural)", "Microsoft Jenny Online (Natural)", "Microsoft Zira"];
  var NOOP_FACE = { setEmotion: function () {}, setMouth: function () {}, setSpeaking: function () {},
    setListening: function () {}, setThinking: function () {} };
  var ACTIVE = ["ready", "work", "rest"];
  var RING = 2 * Math.PI * 52;

  function face() {
    return IP.coachFace || NOOP_FACE;
  }
  function now() {
    return window.performance && performance.now ? performance.now() : Date.now();
  }
  function clamp(v, lo, hi) {
    return v < lo ? lo : v > hi ? hi : v;
  }
  function fmt(sec) {
    sec = Math.max(0, Math.round(sec || 0));
    var m = Math.floor(sec / 60);
    var s = sec % 60;
    return m + ":" + (s < 10 ? "0" : "") + s;
  }
  function cap(s) {
    s = String(s || "");
    return s ? s.charAt(0).toUpperCase() + s.slice(1) : s;
  }
  function readJSON(el, name) {
    try {
      return JSON.parse(el.getAttribute(name) || "null");
    } catch (e) {
      return null;
    }
  }
  function loadPrefs() {
    var p = { voice: true, mic: true };
    try {
      var saved = JSON.parse(window.localStorage.getItem(PREFS_KEY) || "{}") || {};
      if (typeof saved.voice === "boolean") p.voice = saved.voice;
      if (typeof saved.mic === "boolean") p.mic = saved.mic;
    } catch (e) {}
    return p;
  }
  function savePrefs(p) {
    try {
      window.localStorage.setItem(PREFS_KEY, JSON.stringify({ voice: !!p.voice, mic: !!p.mic }));
    } catch (e) {}
  }
  function toast(title, body, tone) {
    if (IP.island && IP.island.notify) IP.island.notify({ title: title, body: body || "", tone: tone || "info" });
  }
  function words(s) {
    return String(s || "").toLowerCase().replace(/[^a-z0-9' ]+/g, " ").split(/\s+/).filter(Boolean);
  }
  /** True when the recogniser most likely heard the coach's own voice through the speakers. */
  function isEcho(heard, said) {
    var h = words(heard);
    if (h.length < 2) return false;
    var bag = {};
    words(said).forEach(function (w) {
      bag[w] = 1;
    });
    var hits = h.filter(function (w) {
      return bag[w];
    }).length;
    return hits / h.length >= 0.6;
  }
  // Microphone / voice instructions are handled right here in the browser (no server round trip).
  var LOCAL_COMMANDS = [
    ["mic_off", /\b(stop listening|(turn|switch|shut) (off|down) (the |your )?(mic|microphone)|(mic|microphone) off|mute (the |my )?(mic|microphone))\b/i],
    ["voice_off", /\b(be quiet|stop talking|shut up|quiet please|voice off|(turn|switch) off (the |your )?voice|mute (yourself|your voice|the voice|voice))\b/i],
    ["voice_on", /\b(voice on|unmute( yourself| the voice| voice)?|(turn|switch) on (the |your )?voice|talk to me again|start talking)\b/i],
  ];
  function localCommand(text) {
    for (var i = 0; i < LOCAL_COMMANDS.length; i++) if (LOCAL_COMMANDS[i][1].test(text || "")) return LOCAL_COMMANDS[i][0];
    return null;
  }
  /** The text as the server's repeat check sees it: case, spacing and surrounding punctuation don't matter. */
  function normText(text) {
    return String(text || "").toLowerCase().replace(/\s+/g, " ").replace(/^[\s.,!?;:'"\u2026-]+|[\s.,!?;:'"\u2026-]+$/g, "");
  }
  // While this network is rate limited (HTTP 429 without a code) only possible health problems still go to the server
  // (deliberately broad - the server's safety layer decides); the core workout commands then run right here.
  var UNSAFE_RX = /pain|hurt|ach(e|ing)|sore|cramp|injur|sprain|strain|pulled|tweak|twist|pop|snap|tear|tore|torn|chest|heart|breath|dizz|faint|pass(ed|ing)? out|black(ed|ing)? out|collaps|sick|nause|vomit|throw(ing)? up|threw up|puk|numb|tingl|weak|head|migraine|vision|blur|seiz|stroke|slur|sugar|hypo|shak|sweat|heat|swell|swoll|bleed|blood|help|emergenc|ambulance|doctor|ouch|unwell|wrong|knee|back|wrist|ankle|neck|shoulder|stomach|\bill\b|not (feeling )?(well|right|good)/i;
  function looksUnsafe(text) {
    return UNSAFE_RX.test(String(text || ""));
  }
  var GATE_COMMANDS = [
    ["next", /^(next|skip|next exercise|skip (it|this|this one)|move on)$/],
    ["pause", /^(pause|hold on|wait|(stop|give me) (for )?a (sec|second|minute))$/],
    ["resume", /^(resume|continue|go|let'?s go|keep going|i'?m ready|start)$/],
    ["done", /^(done|set done|next set|finished|done with (this|the) set)$/],
  ];
  function gateCommand(text) {
    var n = normText(text);
    for (var i = 0; i < GATE_COMMANDS.length; i++) if (GATE_COMMANDS[i][1].test(n)) return GATE_COMMANDS[i][0];
    return null;
  }
  /** A wait the coach can say out loud (IP.ai.formatWait's "2 min" is for the eyes). */
  function spokenWait(seconds) {
    var s = Math.max(1, Math.ceil(Number(seconds) || 0));
    if (s < 60) return s + (s === 1 ? " second" : " seconds");
    var m = Math.ceil(s / 60);
    if (m < 60) return m + (m === 1 ? " minute" : " minutes");
    var h = Math.max(1, Math.round(s / 3600));
    return h === 1 ? "an hour" : h + " hours";
  }
  function formatWait(seconds) {
    return IP.ai && IP.ai.formatWait ? IP.ai.formatWait(seconds) : Math.ceil(seconds) + " s";
  }
  /** AI requests go through IP.ai (per-feature cooldowns, aborted on pagehide); plain IP.api if it is missing. */
  function aiRequest(url, opts) {
    if (IP.ai && IP.ai.request) return IP.ai.request(url, opts);
    return IP.api(url, { method: "POST", json: opts.json, signal: opts.signal });
  }
  function cooldownLeft(feature) {
    return IP.ai && IP.ai.cooldownLeft ? IP.ai.cooldownLeft(feature) : 0;
  }
  function isLogoutForm(form) {
    if (!form || String(form.tagName || "").toUpperCase() !== "FORM") return false;
    var action = form.getAttribute("action") || "";
    try {
      return new URL(action, location.href).pathname === "/auth/logout";
    } catch (e) {
      return /^\/auth\/logout(?:[?#]|$)/.test(action);
    }
  }
  function svgIcon(kind) {
    var ns = "http://www.w3.org/2000/svg";
    var svg = document.createElementNS(ns, "svg");
    svg.setAttribute("viewBox", "0 0 24 24");
    svg.setAttribute("fill", "none");
    svg.setAttribute("stroke", "currentColor");
    svg.setAttribute("stroke-width", "2");
    svg.setAttribute("stroke-linecap", "round");
    svg.setAttribute("stroke-linejoin", "round");
    svg.setAttribute("aria-hidden", "true");
    svg.setAttribute("class", "size-4 mt-0.5 shrink-0 text-ok");
    if (kind === "check") {
      var p = document.createElementNS(ns, "path");
      p.setAttribute("d", "M22 11.08V12a10 10 0 1 1-5.93-9.14");
      var pl = document.createElementNS(ns, "polyline");
      pl.setAttribute("points", "22 4 12 14.01 9 11.01");
      svg.appendChild(p);
      svg.appendChild(pl);
    }
    return svg;
  }

  /* ================================================================ start screen */
  IP.register("live-start", function (root) {
    var form = root.querySelector("[data-live-form]");
    if (!form) return;
    form.setAttribute("data-no-busy", "");
    var startBtn = root.querySelector("[data-start-btn]");
    var label = root.querySelector("[data-selection]");
    var errEl = root.querySelector("[data-start-error]");
    var prefs = loadPrefs();

    root.querySelectorAll("[data-pref]").forEach(function (sw) {
      var key = sw.getAttribute("data-pref");
      if (key === "mic" && !SR) {
        sw.checked = false;
        sw.disabled = true;
        return;
      }
      sw.checked = prefs[key] !== false;
      sw.addEventListener("change", function () {
        prefs[key] = sw.checked;
        savePrefs(prefs);
      });
    });
    if (!SR) {
      root.querySelectorAll("[data-mic-unsupported]").forEach(function (el) {
        el.classList.remove("hidden");
      });
    }

    function picked() {
      return form.querySelector('input[name="pick"]:checked');
    }
    function update() {
      var p = picked();
      var opt = p && p.closest(".live-option");
      if (label) label.textContent = opt ? "Selected: " + (opt.getAttribute("data-title") || "") : "Pick a workout below";
      if (startBtn) startBtn.disabled = !p;
    }
    form.addEventListener("change", update);
    update();

    var dot = root.querySelector("[data-tts-dot]");
    var ttsLabel = root.querySelector("[data-tts-label]");
    IP.api("/api/tts/status?probe=1")
      .then(function (s) {
        if (!s || !dot || !ttsLabel) return;
        var up = s.state === "up";
        dot.setAttribute("data-state", up ? "online" : s.state === "unknown" ? "degraded" : "offline");
        ttsLabel.textContent = up ? "Natural Kokoro voice ready" : "Soft browser voice (Kokoro offline)";
      })
      .catch(function () {});

    function showErr(msg) {
      if (!errEl) return;
      errEl.textContent = msg;
      errEl.classList.remove("hidden");
    }

    form.addEventListener("submit", async function (e) {
      e.preventDefault();
      var p = picked();
      if (!p) {
        showErr("Pick a workout first.");
        return;
      }
      var body = { voice_enabled: prefs.voice !== false };
      if (p.value.indexOf("quick-") === 0) body.quick = p.value.slice(6);
      else {
        var plan = form.querySelector('input[name="plan_id"]');
        body.plan_id = parseInt(plan && plan.value, 10);
        body.day_index = parseInt(p.value.slice(4), 10);
      }
      var spinner = IP.h("span", { class: "spinner", "aria-hidden": "true" });
      startBtn.disabled = true;
      startBtn.setAttribute("aria-busy", "true");
      startBtn.insertBefore(spinner, startBtn.firstChild);
      if (errEl) errEl.classList.add("hidden");
      try {
        var res = await IP.api("/api/live/sessions", { method: "POST", json: body });
        window.location.assign(res.url || "/me/live/" + res.session.id);
      } catch (err) {
        showErr((err && err.message) || "Couldn't start the session - please try again.");
        startBtn.disabled = false;
        startBtn.removeAttribute("aria-busy");
        spinner.remove();
      }
    });
  });

  /* ================================================================ live session */
  IP.register("live-session", function (root) {
    var session = readJSON(root, "data-session") || { items: [], current_index: 0 };
    var cfg = readJSON(root, "data-config") || {};
    var urls = cfg.urls || {};
    var prefs = loadPrefs();
    function q(sel) {
      return root.querySelector(sel);
    }
    function btn(act) {
      return root.querySelector('[data-act="' + act + '"]');
    }
    var el = {
      status: q("[data-status]"), statusText: q("[data-status-text]"), progress: q("[data-progress]"),
      progressBar: q("[data-progress-bar]"), safety: q("[data-safety]"), safetyReason: q("[data-safety-reason]"),
      safetyText: q("[data-safety-text]"), safetyCall: q("[data-safety-call]"), okNote: q("[data-ok-note]"),
      workout: q("[data-workout]"), voiceChip: q("[data-voice-state]"), voiceText: q("[data-voice-text]"),
      intensity: q("[data-intensity]"), intensityLabel: q("[data-intensity-label]"), faceWrap: q("[data-face-wrap]"),
      thinking: q("[data-thinking]"), caption: q("[data-caption]"), interim: q("[data-interim]"),
      readyOverlay: q("[data-ready]"), doneOverlay: q("[data-done]"), main: q("[data-live-main]"),
      summaryTitle: q("[data-summary-title]"), summaryTime: q("[data-summary-time]"), summaryEx: q("[data-summary-ex]"),
      summarySets: q("[data-summary-sets]"), exPos: q("[data-ex-position]"), exCat: q("[data-ex-category]"),
      exName: q("[data-ex-name]"), exSwap: q("[data-ex-swap]"), exMetric: q("[data-ex-metric]"), exUnit: q("[data-ex-unit]"),
      exSetLabel: q("[data-ex-setlabel]"), exSets: q("[data-ex-sets]"), exLoad: q("[data-ex-load]"),
      exLoadText: q("[data-ex-load-text]"), exCues: q("[data-ex-cues]"), setDoneLabel: q("[data-setdone-label]"),
      ring: q("[data-ring]"), ringValue: q("[data-ring-value]"), timer: q("[data-timer]"), phaseLabel: q("[data-phase-label]"),
      nextName: q("[data-next-name]"), nextDose: q("[data-next-dose]"), panel: q("[data-discomfort-panel]"),
      note: q("[data-discomfort-note]"), warn: q("[data-discomfort-warn]"), log: q("[data-log]"), logEmpty: q("[data-log-empty]"),
      upsell: q("[data-upsell]"), chatForm: q("[data-chat-form]"), chatInput: q("[data-chat-input]"),
    };
    if (!el.caption || !el.timer) return;

    var st = {
      phase: "idle", // idle | ready | work | rest | paused | wait | done | safety
      prev: "work",
      set: 1,
      start: now(),
      acc: 0,
      dur: 0,
      begun: false,
      voiceOn: prefs.voice !== false && session.voice_enabled !== false,
      micOn: false,
      micDenied: false,
      setsDone: 0,
      fired: {},
      lastReply: "",
    };
    var thinkingOn = false;
    var baselineTimer = 0;
    var pending = 0; // requests the coach is "thinking" about
    var msg = null; // the AI message in flight {ctrl, key}: a newer message replaces it - never two replies at once
    var chatHeld = false; // AI chat was paused when last painted (an IP.ai cooldown or the network's limit)
    var noting = false; // true while a refused reply is handed to IP.ai: never the network's rate limit
    var gateUntil = 0; // Date.now() until which this network may not post messages (HTTP 429 without a code)
    var pauseNotedAt = 0;
    var lastIntensity = 0;
    var loggingOut = false;

    function items() {
      return session.items || [];
    }
    function idx() {
      return Math.min(session.current_index || 0, items().length);
    }
    function cur() {
      return items()[idx()] || null;
    }
    function eff(item) {
      return (item && item.effective) || {};
    }
    function setsOf(item) {
      return eff(item).sets || (item && item.sets) || 1;
    }
    function elapsed() {
      if (ACTIVE.indexOf(st.phase) === -1) return st.acc;
      return st.acc + (now() - st.start) / 1000;
    }

    /* ---------------------------------------------------------- emotions */
    function baseline() {
      return { idle: "happy", ready: "encouraging", work: "focused", rest: "calm", paused: "neutral", wait: "focused",
        done: "proud", safety: "concerned" }[st.phase] || "neutral";
    }
    function setEmotion(name) {
      clearTimeout(baselineTimer);
      if (name) face().setEmotion(name);
    }
    function scheduleBaseline(ms) {
      clearTimeout(baselineTimer);
      baselineTimer = setTimeout(function () {
        if (!speaker.speaking && !thinkingOn) face().setEmotion(baseline());
      }, ms == null ? 2600 : ms);
    }

    /* ---------------------------------------------------------- voice out (Kokoro TTS / browser voice) */
    var speaker = (function () {
      var queue = [];
      var speaking = false;
      var token = 0;
      var audio = null;
      var actx = null;
      var analyser = null;
      var tbuf = null;
      var fbuf = null;
      var raf = 0;
      var mode = "procedural";
      var cache = {};
      var order = [];
      var downUntil = 0;
      var unlocked = false;
      var voice = null;
      var lastSaid = "";
      var lastEnd = 0;
      var watchdog = 0;
      var ttsCtrl = null; // the voice line being downloaded (stop() drops it)
      var pulseV = 0;
      var pulseRound = 0;

      if (window.speechSynthesis && window.speechSynthesis.addEventListener) {
        window.speechSynthesis.addEventListener("voiceschanged", function () {
          voice = null;
        });
      }

      function silentWav() {
        var n = 800;
        var buf = new ArrayBuffer(44 + n * 2);
        var v = new DataView(buf);
        function str(o, s) {
          for (var i = 0; i < s.length; i++) v.setUint8(o + i, s.charCodeAt(i));
        }
        str(0, "RIFF");
        v.setUint32(4, 36 + n * 2, true);
        str(8, "WAVE");
        str(12, "fmt ");
        v.setUint32(16, 16, true);
        v.setUint16(20, 1, true);
        v.setUint16(22, 1, true);
        v.setUint32(24, 8000, true);
        v.setUint32(28, 16000, true);
        v.setUint16(32, 2, true);
        v.setUint16(34, 16, true);
        str(36, "data");
        v.setUint32(40, n * 2, true);
        return new Blob([buf], { type: "audio/wav" });
      }

      function unlock() {
        if (unlocked) return;
        unlocked = true;
        try {
          var AC = window.AudioContext || window.webkitAudioContext;
          if (AC) {
            actx = new AC();
            if (actx.resume) actx.resume();
          }
        } catch (e) {
          actx = null;
        }
        try {
          audio = new Audio();
          audio.preload = "auto";
          audio.setAttribute("playsinline", "");
          if (actx) {
            var src = actx.createMediaElementSource(audio);
            analyser = actx.createAnalyser();
            analyser.fftSize = 1024;
            analyser.smoothingTimeConstant = 0.55;
            src.connect(analyser);
            analyser.connect(actx.destination);
            tbuf = new Uint8Array(analyser.fftSize);
            fbuf = new Uint8Array(analyser.frequencyBinCount);
          }
          audio.src = URL.createObjectURL(silentWav());
          var p = audio.play();
          if (p && p.catch) p.catch(function () {});
        } catch (e) {
          analyser = null;
        }
        if (window.speechSynthesis && typeof SpeechSynthesisUtterance !== "undefined") {
          try {
            var u = new SpeechSynthesisUtterance(" ");
            u.volume = 0;
            window.speechSynthesis.speak(u);
          } catch (e) {}
        }
      }

      function pickVoice() {
        if (voice) return voice;
        var synth = window.speechSynthesis;
        if (!synth || !synth.getVoices) return null;
        var list = synth.getVoices() || [];
        for (var i = 0; i < SOFT_VOICES.length; i++) {
          for (var j = 0; j < list.length; j++) {
            if (list[j].name === SOFT_VOICES[i] || list[j].name.indexOf(SOFT_VOICES[i]) === 0) {
              voice = list[j];
              return voice;
            }
          }
        }
        var en = list.filter(function (v) {
          return /^en([-_]|$)/i.test(v.lang || "");
        });
        voice = en.filter(function (v) {
          return /female|samantha|victoria|serena|fiona|zira|aria|jenny|libby|sonia/i.test(v.name);
        })[0] || en[0] || list[0] || null;
        return voice;
      }

      /** No new voice requests: the engine is down, a refusal's Retry-After runs, or AI is paused page-wide. */
      function voicePaused() {
        return now() < downUntil || cooldownLeft("tts") > 0;
      }
      /** 429 (voice quota / AI paused) or 503 (engine down, voice lane busy): the browser voice until Retry-After. */
      function backOff(err) {
        if (!err || err.name === "AbortError" || (err.status !== 429 && err.status !== 503)) return;
        downUntil = Math.max(downUntil, now() + (err.retryAfter > 0 ? err.retryAfter : 60) * 1000);
      }

      function fetchTts(text) {
        if (cache[text]) return Promise.resolve(cache[text]);
        var ctrl = window.AbortController ? new AbortController() : null;
        var timer = setTimeout(function () {
          if (ctrl) ctrl.abort();
        }, 9000);
        ttsCtrl = ctrl;
        function settle() {
          clearTimeout(timer);
          if (ttsCtrl === ctrl) ttsCtrl = null;
        }
        var headers = { "Content-Type": "application/json", Accept: "audio/mpeg", "X-CSRF-Token": IP.csrf() };
        if (IP.pageId) headers["X-Page-Id"] = IP.pageId; // the server stops this tab's voice work when it closes
        return fetch(urls.tts, {
          method: "POST",
          credentials: "same-origin",
          headers: headers,
          body: JSON.stringify({ text: text.slice(0, 400) }),
          signal: ctrl ? ctrl.signal : undefined,
        })
          .then(function (res) {
            settle();
            var ct = res.headers.get("content-type") || "";
            if (!res.ok || ct.indexOf("audio") === -1) {
              var err = new Error("tts " + res.status);
              err.status = res.status;
              err.retryAfter = IP.parseRetryAfter ? IP.parseRetryAfter(res.headers.get("retry-after")) : null;
              throw err;
            }
            return res.blob();
          }, function (err) {
            settle();
            throw err;
          })
          .then(function (blob) {
            var url = URL.createObjectURL(blob);
            cache[text] = url;
            order.push(text);
            if (order.length > 40) {
              var old = order.shift();
              if (cache[old]) URL.revokeObjectURL(cache[old]);
              delete cache[old];
            }
            return url;
          });
      }

      function setSpeakingUI(on) {
        face().setSpeaking(on);
        if (el.faceWrap) el.faceWrap.setAttribute("data-speaking", on ? "1" : "0");
        voiceState();
      }

      function loop() {
        raf = 0;
        if (!speaking) {
          face().setMouth(0, 0);
          return;
        }
        var v = 0;
        var r = 0;
        if (mode === "audio" && analyser) {
          analyser.getByteTimeDomainData(tbuf);
          var sum = 0;
          for (var i = 0; i < tbuf.length; i++) {
            var x = (tbuf[i] - 128) / 128;
            sum += x * x;
          }
          v = clamp((Math.sqrt(sum / tbuf.length) - 0.012) * 5.5, 0, 1);
          analyser.getByteFrequencyData(fbuf);
          var num = 0;
          var den = 0;
          for (var j = 2; j < fbuf.length / 4; j++) {
            num += j * fbuf[j];
            den += fbuf[j];
          }
          if (den > 0 && v > 0.12) {
            var hz = (num / den) * (actx.sampleRate / analyser.fftSize);
            r = clamp(1 - (hz - 500) / 1300, 0, 1);
          }
        } else {
          var t = now() / 1000;
          pulseV *= 0.86;
          pulseRound *= 0.9;
          v = clamp(0.16 + 0.22 * Math.abs(Math.sin(t * 11.3)) + 0.14 * Math.abs(Math.sin(t * 7.1 + 1.3)) + pulseV, 0, 1);
          r = pulseRound;
        }
        face().setMouth(v, r);
        raf = window.requestAnimationFrame(loop);
      }
      function lipSync(m) {
        mode = m;
        if (!raf) raf = window.requestAnimationFrame(loop);
      }
      function pulse(word) {
        pulseV = 0.42;
        pulseRound = /oo|ou|o|u|w/i.test(word || "") ? 0.85 : 0;
      }

      function finish() {
        clearTimeout(watchdog);
        if (!speaking) return;
        speaking = false;
        lastEnd = now();
        setSpeakingUI(false);
        face().setMouth(0, 0);
        if (queue.length) setTimeout(next, 220);
        else scheduleBaseline();
      }

      function browserSay(text, my) {
        var synth = window.speechSynthesis;
        clearTimeout(watchdog);
        if (!synth || typeof SpeechSynthesisUtterance === "undefined") {
          lipSync("procedural");
          watchdog = setTimeout(function () {
            if (my === token) finish();
          }, Math.min(9000, 600 + text.length * 55));
          return;
        }
        try {
          synth.cancel();
        } catch (e) {}
        var u = new SpeechSynthesisUtterance(text);
        var v = pickVoice();
        if (v) {
          u.voice = v;
          u.lang = v.lang;
        }
        u.rate = 0.95;
        u.pitch = 1.05;
        u.onboundary = function (e) {
          if (my !== token) return;
          var m = text.slice(e.charIndex || 0).match(/^[\w']+/);
          pulse(m ? m[0] : "");
        };
        u.onend = function () {
          if (my === token) finish();
        };
        u.onerror = function () {
          if (my === token) finish();
        };
        lipSync("procedural");
        synth.speak(u);
        watchdog = setTimeout(function () {
          if (my === token) finish();
        }, Math.min(25000, 2000 + text.length * 95));
      }

      function playUrl(url, my, text) {
        if (!audio) {
          browserSay(text, my);
          return;
        }
        audio.onended = function () {
          if (my === token) finish();
        };
        audio.onerror = function () {
          if (my === token) browserSay(text, my);
        };
        audio.src = url;
        if (actx && actx.state === "suspended" && actx.resume) actx.resume();
        var p = audio.play();
        if (p && p.catch) {
          p.catch(function () {
            if (my === token) browserSay(text, my);
          });
        }
        lipSync(analyser ? "audio" : "procedural");
        watchdog = setTimeout(function () {
          if (my === token) finish();
        }, 30000);
      }

      function next() {
        if (speaking) return;
        var item = queue.shift();
        if (!item) return;
        var my = ++token;
        lastSaid = item.text;
        el.caption.textContent = item.text;
        el.caption.setAttribute("data-tone", item.alert ? "alert" : "");
        if (item.emotion) setEmotion(item.emotion);
        if (!st.voiceOn) {
          lastEnd = now();
          scheduleBaseline(3200);
          if (queue.length) setTimeout(next, 900);
          return;
        }
        speaking = true;
        setSpeakingUI(true);
        if (!urls.tts || (!cache[item.text] && voicePaused())) {
          browserSay(item.text, my); // lines downloaded before still play in the natural voice
          return;
        }
        fetchTts(item.text)
          .then(function (url) {
            if (my === token) playUrl(url, my, item.text);
          })
          .catch(function (err) {
            backOff(err);
            if (my === token) browserSay(item.text, my);
          });
      }

      function say(text, emotion, opts) {
        text = String(text || "").trim();
        opts = opts || {};
        if (!text) {
          if (emotion) setEmotion(emotion);
          return;
        }
        if (opts.high) stop();
        else if (opts.low && (speaking || queue.length)) return; // drop optional cues when busy
        queue.push({ text: text, emotion: emotion || null, alert: !!opts.alert });
        if (queue.length > 4) queue.splice(0, queue.length - 4);
        if (!speaking) next();
      }

      function stop() {
        queue.length = 0;
        token++;
        clearTimeout(watchdog);
        if (ttsCtrl) {
          var c = ttsCtrl;
          ttsCtrl = null;
          try {
            c.abort();
          } catch (e) {}
        }
        if (audio) {
          try {
            audio.pause();
          } catch (e) {}
        }
        if (window.speechSynthesis) {
          try {
            window.speechSynthesis.cancel();
          } catch (e) {}
        }
        if (speaking) {
          speaking = false;
          lastEnd = now();
          setSpeakingUI(false);
          face().setMouth(0, 0);
        }
      }

      return {
        say: say,
        stop: stop,
        unlock: unlock,
        get speaking() {
          return speaking;
        },
        lastSaid: function () {
          return lastSaid;
        },
        lastEnd: function () {
          return lastEnd;
        },
        ctx: function () {
          return actx;
        },
      };
    })();

    function say(text, emotion, opts) {
      speaker.say(text, emotion, opts);
    }

    function beep(freq, dur) {
      var ctx = speaker.ctx();
      if (!st.voiceOn || !ctx) return;
      try {
        var t = ctx.currentTime;
        var o = ctx.createOscillator();
        var g = ctx.createGain();
        o.type = "sine";
        o.frequency.value = freq;
        g.gain.setValueAtTime(0.0001, t);
        g.gain.exponentialRampToValueAtTime(0.16, t + 0.015);
        g.gain.exponentialRampToValueAtTime(0.0001, t + dur);
        o.connect(g);
        g.connect(ctx.destination);
        o.start(t);
        o.stop(t + dur + 0.03);
      } catch (e) {}
    }
    function vibrate(ms) {
      try {
        if (navigator.vibrate) navigator.vibrate(ms);
      } catch (e) {}
    }

    /* ---------------------------------------------------------- voice in (speech recognition) */
    var rec = null;
    var recMode = null;
    var recTimer = 0;
    var recFails = 0;

    function startRec(mode) {
      if (!SR || rec || st.micDenied) return;
      var r;
      try {
        r = new SR();
      } catch (e) {
        return;
      }
      r.continuous = mode === "continuous";
      r.interimResults = true;
      r.maxAlternatives = 1;
      r.lang = navigator.language || "en-US";
      rec = r;
      recMode = mode;
      r.onstart = function () {
        recFails = 0;
        voiceState();
      };
      r.onresult = onResult;
      r.onerror = function (e) {
        var err = e && e.error;
        if (err === "not-allowed" || err === "service-not-allowed" || err === "audio-capture") {
          st.micDenied = true;
          setMic(false, true);
          toast(err === "audio-capture" ? "No microphone found" : "Microphone blocked",
            "Allow microphone access to talk to Pulse - or type instead.", "warning");
          addNote("Microphone unavailable - type your messages instead.");
        } else if (err === "network") {
          recFails++;
        }
      };
      r.onend = function () {
        if (rec === r) rec = null;
        el.interim.textContent = "";
        voiceState();
        if (mode === "continuous" && st.micOn && !st.micDenied && st.phase !== "safety" && st.phase !== "done") {
          clearTimeout(recTimer);
          recTimer = setTimeout(function () {
            if (st.micOn && !rec) startRec("continuous");
          }, Math.min(6000, 300 * Math.pow(2, recFails)));
        }
      };
      try {
        r.start();
      } catch (e) {
        rec = null;
      }
      voiceState();
    }

    function stopRec() {
      clearTimeout(recTimer);
      if (rec) {
        var r = rec;
        rec = null;
        r.onend = null;
        try {
          r.abort();
        } catch (e) {}
      }
      el.interim.textContent = "";
      voiceState();
    }

    function onResult(e) {
      var fin = "";
      var interim = "";
      for (var i = e.resultIndex; i < e.results.length; i++) {
        var res = e.results[i];
        var t = (res[0] && res[0].transcript) || "";
        if (res.isFinal) fin += t;
        else interim += t;
      }
      var recent = speaker.speaking || now() - speaker.lastEnd() < 1500;
      if (interim.trim()) {
        if (recent && isEcho(interim, speaker.lastSaid())) return;
        el.interim.textContent = "“" + interim.trim() + "”";
        if (speaker.speaking && interim.trim().length > 2) speaker.stop(); // barge-in
      }
      if (fin.trim()) {
        el.interim.textContent = "";
        if (recent && isEcho(fin, speaker.lastSaid())) return;
        sendMessage(fin.trim(), "voice");
      }
    }

    function setMic(on, silent) {
      on = !!on && !!SR && !st.micDenied;
      st.micOn = on;
      if (!silent) {
        prefs.mic = on;
        savePrefs(prefs);
      }
      var b = btn("mic");
      if (b) {
        b.setAttribute("aria-pressed", on ? "true" : "false");
        b.setAttribute("aria-label", on ? "Turn microphone off" : "Turn microphone on");
        var a = b.querySelector("[data-icon-on]");
        var o = b.querySelector("[data-icon-off]");
        if (a) a.hidden = !on;
        if (o) o.hidden = on;
      }
      if (on && st.begun && st.phase !== "safety" && st.phase !== "done") startRec("continuous");
      else if (!on && recMode === "continuous") stopRec();
      if (!silent) addNote(on ? "Microphone on - just talk to Pulse." : "Microphone off - type or hold to talk.");
      voiceState();
    }

    function setVoice(on, silent) {
      st.voiceOn = !!on;
      if (!silent) {
        prefs.voice = st.voiceOn;
        savePrefs(prefs);
      }
      var b = btn("voice");
      if (b) {
        b.setAttribute("aria-pressed", on ? "true" : "false");
        b.setAttribute("aria-label", on ? "Mute the AI voice" : "Unmute the AI voice");
        var a = b.querySelector("[data-icon-on]");
        var o = b.querySelector("[data-icon-off]");
        if (a) a.hidden = !on;
        if (o) o.hidden = on;
      }
      if (!on) speaker.stop();
      if (!silent) addNote(on ? "AI voice on." : "AI voice muted - captions stay on.");
    }

    function voiceState() {
      var state = "off";
      var text = st.micOn ? "Mic starting…" : SR ? "Mic off" : "Type to talk";
      if (st.phase === "safety") {
        state = "alert";
        text = "Safety stop";
      } else if (thinkingOn) {
        state = "thinking";
        text = "Thinking…";
      } else if (speaker && speaker.speaking) {
        state = "speaking";
        text = "Speaking";
      } else if (rec) {
        state = "listening";
        text = recMode === "ptt" ? "Listening (hold)" : "Listening";
      } else if (st.phase === "idle") {
        text = "Coach ready";
      }
      if (el.voiceChip) el.voiceChip.setAttribute("data-state", state);
      if (el.voiceText) el.voiceText.textContent = text;
      if (el.faceWrap) el.faceWrap.setAttribute("data-listening", state === "listening" ? "1" : "0");
      face().setListening(state === "listening");
    }

    function thinking(on) {
      thinkingOn = !!on;
      if (el.thinking) el.thinking.hidden = !on;
      face().setThinking(!!on);
      if (on) setEmotion("focused");
      voiceState();
    }

    /* ---------------------------------------------------------- transcript */
    function addMsg(kind, text, meta) {
      if (!el.log || !text) return;
      if (el.logEmpty) {
        el.logEmpty.remove();
        el.logEmpty = null;
      }
      var cls = { user: "live-msg live-msg-user", alert: "live-msg live-msg-alert" }[kind] || "live-msg live-msg-coach";
      var node = IP.h("div", { class: cls }, String(text));
      if (meta) node.appendChild(IP.h("span", { class: "live-msg-meta", text: meta }));
      el.log.appendChild(node);
      while (el.log.children.length > 80) el.log.removeChild(el.log.firstChild);
      el.log.scrollTop = el.log.scrollHeight;
    }
    function addNote(text) {
      if (!el.log || !text) return;
      if (el.logEmpty) {
        el.logEmpty.remove();
        el.logEmpty = null;
      }
      el.log.appendChild(IP.h("p", { class: "live-msg-note", text: text }));
      el.log.scrollTop = el.log.scrollHeight;
    }
    function metaFor(res) {
      return { ai: "Pulse · AI", rule: "Pulse", safety: "Safety", fallback: "Pulse · offline", upsell: "Pulse" }[
        res && res.source
      ] || "Pulse";
    }

    /* ---------------------------------------------------------- rendering */
    function render() {
      var list = items();
      var n = list.length;
      var i = idx();
      var item = cur();
      if (item) {
        var e = eff(item);
        var sets = setsOf(item);
        el.exPos.textContent = "Exercise " + (i + 1) + " of " + n;
        el.exCat.textContent = cap(item.category || "exercise");
        el.exName.textContent = item.exercise || "";
        if (item.swapped_from) {
          el.exSwap.textContent = "Swapped from " + item.swapped_from + (item.swap_reason ? " · " + item.swap_reason : "");
          el.exSwap.classList.remove("hidden");
        } else el.exSwap.classList.add("hidden");
        el.exMetric.textContent = e.reps ? String(e.reps) : e.duration_s ? String(e.duration_s) : "–";
        el.exUnit.textContent = e.reps ? "reps" : "sec";
        el.exSetLabel.textContent = "Set " + Math.min(st.set, sets) + " of " + sets;
        el.exSets.textContent = "";
        for (var k = 1; k <= sets && k <= 12; k++) {
          var state = k < st.set ? "done" : k === st.set && st.phase !== "idle" ? "current" : "";
          el.exSets.appendChild(IP.h("span", { class: "live-set", "data-state": state }));
        }
        if (el.exLoad) {
          if (e.load_note) {
            if (el.exLoadText) el.exLoadText.textContent = e.load_note;
            el.exLoad.classList.remove("hidden");
          } else el.exLoad.classList.add("hidden");
        }
        el.exCues.textContent = "";
        (item.cues || []).slice(0, 3).forEach(function (c) {
          el.exCues.appendChild(IP.h("li", { class: "live-cue" }, svgIcon("check"), IP.h("span", { text: c })));
        });
      } else {
        el.exPos.textContent = "All done";
        el.exCat.textContent = "Done";
        el.exName.textContent = n ? "Workout finished" : "No exercises";
        el.exSwap.classList.add("hidden");
        el.exMetric.textContent = "✓";
        el.exUnit.textContent = "";
        el.exSetLabel.textContent = "";
        el.exSets.textContent = "";
        el.exCues.textContent = "";
        if (el.exLoad) el.exLoad.classList.add("hidden");
      }
      var nx = list[i + 1];
      el.nextName.textContent = nx ? nx.exercise : item ? "Finish" : "—";
      el.nextDose.textContent = nx ? nx.dose || "" : item ? "Cool down and celebrate" : "";
      var level = session.intensity || 0;
      if (el.intensity) {
        Array.prototype.forEach.call(el.intensity.children, function (bar, j) {
          bar.setAttribute("data-on", j - 3 <= level ? "1" : "0");
        });
      }
      if (el.intensityLabel) el.intensityLabel.textContent = session.intensity_label || "Standard";
      var frac = 1;
      if (n && st.phase !== "done") frac = (i + (item ? (Math.min(st.set, setsOf(item)) - 1) / setsOf(item) : 0)) / n;
      el.progressBar.style.width = (clamp(frac, 0, 1) * 100).toFixed(1) + "%";
      el.progress.setAttribute("aria-valuenow", String(Math.round(clamp(frac, 0, 1) * 100)));
      renderControls();
      renderTimer();
      statusChip();
      voiceState();
    }

    function renderControls() {
      var paused = st.phase === "paused" || st.phase === "idle";
      var t = btn("toggle");
      if (t) {
        t.setAttribute("aria-label", st.phase === "idle" ? "Begin workout" : paused ? "Resume" : "Pause");
        var pi = t.querySelector("[data-icon-pause]");
        var pl = t.querySelector("[data-icon-play]");
        if (pi) pi.hidden = paused;
        if (pl) pl.hidden = !paused;
      }
      var label = { idle: "Begin", ready: "Start now", work: "Set complete", rest: "Skip rest", paused: "Resume",
        wait: "Saving…", done: "Finished", safety: "Stopped" }[st.phase] || "Set complete";
      if (el.setDoneLabel) el.setDoneLabel.textContent = label;
      var over = st.phase === "done" || st.phase === "safety";
      ["toggle", "next", "setdone", "discomfort", "easier", "harder", "ptt", "end"].forEach(function (a) {
        var b = btn(a);
        if (b) b.disabled = over || (st.phase === "wait" && a !== "discomfort");
      });
      var ptt = btn("ptt");
      if (ptt && !SR) ptt.disabled = true;
    }

    function renderTimer() {
      var e = elapsed();
      var phase = st.phase === "paused" ? st.prev : st.phase;
      var txt = "0:00";
      var frac = 0;
      var label = "Ready";
      if (st.phase === "done") {
        txt = "✓";
        frac = 1;
        label = "Done";
      } else if (st.phase === "safety") {
        txt = "—";
        label = "Stopped";
      } else if (st.phase === "wait") {
        txt = "…";
        frac = 1;
        label = "Saving";
      } else if (st.phase !== "idle" && st.dur > 0) {
        var rem = Math.max(0, st.dur - e);
        txt = fmt(rem);
        frac = clamp(rem / st.dur, 0, 1);
        label = { ready: "Get ready", rest: "Rest", work: "Work" }[phase] || "Work";
      } else if (st.phase !== "idle") {
        txt = fmt(e);
        frac = clamp(e / Math.max(20, (eff(cur()).reps || 10) * 3.5), 0, 1);
        label = "Work";
      }
      if (st.phase === "paused") label = "Paused";
      el.timer.textContent = txt;
      el.phaseLabel.textContent = label;
      if (el.ringValue) el.ringValue.setAttribute("stroke-dashoffset", (RING * (1 - frac)).toFixed(2));
      if (el.ring) el.ring.setAttribute("data-phase", st.phase === "paused" ? "paused" : st.phase === "idle" ? "ready" : phase);
    }

    function statusChip() {
      var map = { idle: ["off", "Ready"], ready: ["thinking", "Get ready"], work: ["speaking", "Working"],
        rest: ["listening", "Resting"], paused: ["paused", "Paused"], wait: ["thinking", "Finishing…"],
        done: ["off", session.status === "completed" ? "Completed" : "Finished"], safety: ["alert", "Stopped for safety"] };
      var m = map[st.phase] || map.idle;
      if (el.status) el.status.setAttribute("data-state", m[0]);
      if (el.statusText) el.statusText.textContent = m[1];
    }

    /* ---------------------------------------------------------- workout flow */
    function setPhase(phase, dur) {
      st.phase = phase;
      st.dur = dur || 0;
      st.start = now();
      st.acc = 0;
      st.fired = {};
      render();
      scheduleBaseline(1800);
    }

    function startWork() {
      var item = cur();
      if (!item) {
        st.phase = "wait";
        render();
        return;
      }
      var e = eff(item);
      setPhase("work", item.timed ? e.duration_s || 30 : 0);
      var sets = setsOf(item);
      say(st.set === 1 ? item.exercise + ". Go!" : "Set " + st.set + " of " + sets + ". Go!", "focused", { low: true });
      beep(990, 0.18);
      vibrate(60);
    }

    function completeSet() {
      if (st.phase !== "work") return;
      var item = cur();
      if (!item) return;
      var sets = setsOf(item);
      var done = st.set;
      var index = idx();
      st.setsDone++;
      vibrate(40);
      if (done < sets) {
        st.set = done + 1;
        setPhase("rest", eff(item).rest_s || 45);
        sendEvent("set_done", { index: index, set: done });
        return;
      }
      st.set = 1;
      if (index + 1 >= items().length) {
        st.phase = "wait";
        render();
      } else {
        session.current_index = index + 1;
        setPhase("rest", eff(item).rest_s || 45);
      }
      sendEvent("exercise_done", { index: index, set: done });
    }

    function primaryAction() {
      if (!st.begun) return begin();
      if (st.phase === "work") completeSet();
      else if (st.phase === "ready" || st.phase === "rest") startWork();
      else if (st.phase === "paused") togglePause();
    }

    function pauseLocal() {
      if (ACTIVE.indexOf(st.phase) === -1) return false;
      st.acc = elapsed();
      st.prev = st.phase;
      st.phase = "paused";
      render();
      return true;
    }
    function resumeLocal() {
      if (st.phase !== "paused") return false;
      st.phase = st.prev || "work";
      st.start = now();
      render();
      return true;
    }
    function togglePause() {
      if (st.phase === "paused") {
        if (resumeLocal()) sendEvent("resumed");
      } else if (pauseLocal()) sendEvent("paused");
    }

    function skip() {
      if (!st.begun || (ACTIVE.indexOf(st.phase) === -1 && st.phase !== "paused")) return;
      var i = idx();
      speaker.stop();
      sendEvent("skipped", { index: i, set: st.set });
      st.set = 1;
      if (i + 1 >= items().length) {
        st.phase = "wait";
        render();
        return;
      }
      session.current_index = i + 1;
      setPhase("ready", 6);
    }

    function tick() {
      paintChat();
      if (ACTIVE.indexOf(st.phase) === -1) {
        renderTimer();
        return;
      }
      var item = cur();
      var e = elapsed();
      if (st.dur > 0) {
        var rem = st.dur - e;
        var r = Math.ceil(rem);
        if (r >= 1 && r <= 3 && !st.fired["b" + r]) {
          st.fired["b" + r] = 1;
          beep(r === 1 ? 784 : 660, 0.12);
        }
        if (st.phase === "work" && st.dur >= 40 && rem <= st.dur / 2 && !st.fired.half) {
          st.fired.half = 1;
          say("Halfway there - keep it smooth.", "encouraging", { low: true });
        }
        if (st.phase === "work" && st.dur >= 25 && rem <= 10 && rem > 7 && !st.fired.ten) {
          st.fired.ten = 1;
          say("Ten seconds left!", "encouraging", { low: true });
        }
        if (st.phase === "rest" && st.dur >= 30 && rem <= 10 && rem > 7 && !st.fired.ten && item) {
          st.fired.ten = 1;
          say("Ten seconds. Get ready for " + item.exercise + ".", "calm", { low: true });
        }
        if (rem <= 0) {
          if (st.phase === "work") completeSet();
          else startWork();
          return;
        }
      }
      renderTimer();
    }

    function begin() {
      if (st.begun || st.phase === "safety" || st.phase === "done") return;
      st.begun = true;
      speaker.unlock();
      if (el.readyOverlay) el.readyOverlay.hidden = true;
      if (el.main) el.main.hidden = false;
      requestWakeLock();
      st.set = 1;
      setPhase("ready", idx() === 0 ? 8 : 6);
      setMic(st.micOn, true);
      sendEvent("started", { index: idx(), set: 1 });
      var t = btn("toggle");
      if (t) t.focus();
    }

    function enterSafety(a) {
      a = a || {};
      var em = a.emergency || cfg.emergency || {};
      if (el.safetyReason && a.label) el.safetyReason.textContent = a.suspended ? a.label + "." : "You reported " + a.label + ".";
      if (el.safetyText && st.lastReply) el.safetyText.textContent = st.lastReply;
      if (el.safetyCall && em.number) {
        el.safetyCall.setAttribute("href", "tel:" + em.number);
        var span = el.safetyCall.lastChild;
        if (span && span.nodeType === 3) span.textContent = " Call " + em.number;
      }
      if (st.phase === "safety") return;
      st.phase = "safety";
      stopRec();
      releaseWakeLock();
      closePanel();
      if (el.readyOverlay) el.readyOverlay.hidden = true;
      if (el.doneOverlay) el.doneOverlay.hidden = true;
      if (el.main) el.main.hidden = false;
      if (el.safety) el.safety.hidden = false;
      if (el.workout) el.workout.setAttribute("data-mode", "safety");
      setEmotion("concerned");
      render();
      if (el.safety && el.safety.scrollIntoView) el.safety.scrollIntoView({ behavior: "smooth", block: "start" });
      if (el.safetyCall) el.safetyCall.focus({ preventScroll: true });
    }

    function enterDone() {
      if (st.phase === "done" || st.phase === "safety") return;
      st.phase = "done";
      stopRec();
      releaseWakeLock();
      closePanel();
      var completed = session.status === "completed";
      var started = Date.parse(session.started_at || "") || Date.now();
      var ended = Date.parse(session.ended_at || "") || Date.now();
      if (el.summaryTitle) el.summaryTitle.textContent = completed ? "Workout complete!" : "Session ended";
      if (el.summaryTime) el.summaryTime.textContent = String(Math.max(1, Math.round((ended - started) / 60000)));
      if (el.summaryEx) el.summaryEx.textContent = String(completed ? items().length : Math.min(session.current_index || 0, items().length));
      if (el.summarySets) el.summarySets.textContent = String(st.setsDone);
      if (el.readyOverlay) el.readyOverlay.hidden = true;
      if (el.main) el.main.hidden = true;
      if (el.doneOverlay) el.doneOverlay.hidden = false;
      setEmotion("proud");
      render();
    }

    /* ---------------------------------------------------------- server round trips */
    function stateFor(extra) {
      var phase = st.phase === "paused" ? "paused" : st.phase;
      var s = { index: idx(), set: st.set, phase: ["work", "rest", "paused", "ready", "done"].indexOf(phase) >= 0 ? phase : "work",
        elapsed_s: Math.round(elapsed()), voice: st.voiceOn };
      if (extra) {
        Object.keys(extra).forEach(function (k) {
          s[k] = extra[k];
        });
      }
      return s;
    }

    function localCue(type) {
      var item = cur();
      var name = item ? item.exercise : "the next one";
      return { started: "Let's go! First up: " + name + ".", set_done: "Nice work! Take your rest.",
        exercise_done: item ? "Great job! Next up: " + name + "." : "Great job!", skipped: "Okay, skipping. Next up: " + name + ".",
        paused: "Paused.", resumed: "Let's go!" }[type] || "";
    }

    async function sendEvent(type, extra, opts) {
      opts = opts || {};
      try {
        var res = await IP.api(urls.event, { method: "POST", json: { type: type, state: stateFor(extra) } });
        applyServer(res, { event: type, silent: opts.silent });
        return res;
      } catch (err) {
        handleError(err, type);
        if (!opts.silent && (!err || !err.status || err.status >= 500 || err.status === 429)) say(localCue(type), "encouraging");
        if (type === "exercise_done" && st.phase === "wait") enterDone();
        return null;
      }
    }

    /* ---------------------------------------------------------- AI chat pacing (docs/ai-guard-and-queue.md) */
    function gateLeft() {
      return Math.max(0, Math.ceil((gateUntil - Date.now()) / 1000));
    }
    /** Seconds AI chat stays paused: IP.ai's "live_coach" cooldown - a refused reply here, a 429, or AI paused for the
     * whole account by any AI widget of this page. */
    function chatPauseLeft() {
      return cooldownLeft("live_coach");
    }
    /** A refused reply: IP.ai keeps the wait (one timer for the whole page), as if the server had answered 429. */
    function holdChat(refused) {
      if (!IP.ai || !IP.ai.noteRefusal) return;
      noting = true;
      try {
        IP.ai.noteRefusal("live_coach", refused.code, refused.retry_after);
      } finally {
        noting = false;
      }
      paintChat();
    }
    var placeholder = el.chatInput ? el.chatInput.getAttribute("placeholder") || "" : "";
    var painted = placeholder;
    /** The chat box's countdown; says once that chat is back when a pause (whoever started it) is over. */
    function paintChat() {
      var left = Math.max(chatPauseLeft(), gateLeft());
      if (left > 0) chatHeld = true;
      else if (chatHeld) {
        chatHeld = false;
        if (st.phase !== "safety" && st.phase !== "done") addNote("Pulse can chat again.");
      }
      if (!el.chatInput) return;
      var text = left > 0 ? "Chat is back in " + formatWait(left) + " - commands still work" : placeholder;
      if (text === painted) return;
      painted = text;
      el.chatInput.setAttribute("placeholder", text);
    }
    function notePaused(seconds, gate) {
      if (Date.now() - pauseNotedAt < 20000) return; // one note per 20 s, however much is said meanwhile
      pauseNotedAt = Date.now();
      var wait = formatWait(seconds) || "a moment";
      addNote(gate ? "Lots of messages from this network - chat is back in " + wait + ". The workout buttons still work."
        : "Chat with Pulse is back in " + wait + " - say next, pause or easier any time.");
    }
    function busy(delta) {
      pending = Math.max(0, pending + delta);
      if (thinkingOn !== pending > 0) thinking(pending > 0);
    }
    function abortMessage() {
      var m = msg;
      msg = null;
      if (m && m.ctrl) {
        try {
          m.ctrl.abort();
        } catch (e) {}
      }
    }
    /** Commands this page can run without the message endpoint (it is rate limited for this network). */
    function runLocal(cmd) {
      if (!cmd || !st.begun || st.phase === "safety" || st.phase === "done") return false;
      if (cmd === "next") skip();
      else if (cmd === "pause") {
        if (pauseLocal()) sendEvent("paused");
      } else if (cmd === "resume") {
        if (resumeLocal()) sendEvent("resumed");
      } else primaryAction();
      return true;
    }
    /** The reply to an AI message: a refusal is spoken once (nothing for a repeat, which pauses nothing - the first
     * answer is on its way) and pauses AI chat through IP.ai - before the line is spoken, so an account-wide pause
     * already keeps the natural voice from asking. */
    function applyReply(res) {
      var refused = (res && res.refused) || null;
      if (refused && refused.code === "duplicate") {
        if (res.session) {
          session = res.session; // the first answer is (or was) on its way - say nothing
          render();
        }
        return;
      }
      if (refused && refused.retry_after > 0) holdChat(refused);
      applyServer(res, { chat: true });
    }
    /** AI chat is paused: the server still runs every safety check and command on these words - without the AI. */
    async function sendPlain(text, via, unsafe) {
      busy(1);
      try {
        var res = await aiRequest(urls.message, {
          json: { text: text, via: via === "voice" ? "voice" : "text", state: stateFor(), ai: false },
          feature: "live_coach",
          safety: true,
          timeout: 12000,
        });
        busy(-1);
        if (res && res.chat_paused && !res.reply) {
          if (res.session) {
            session = res.session;
            render();
          }
          showMemory(res);
          notePaused(Math.max(chatPauseLeft(), gateLeft()), gateLeft() > 0);
        } else applyServer(res, { chat: true });
      } catch (err) {
        busy(-1);
        var status = err && err.status;
        if (unsafe && status !== 401 && status !== 403 && status !== 404 && st.phase !== "safety" && st.phase !== "done") {
          // these words may describe a health problem and could not be checked: straight to the discomfort report
          addNote("Couldn't reach Pulse - if something hurts or feels wrong, tell me where.");
          openPanel();
        } else handleError(err, "message");
      }
    }

    async function sendMessage(text, via, opts) {
      text = String(text || "").trim().slice(0, 500);
      opts = opts || {};
      if (!text) return;
      var local = localCommand(text);
      if (local) {
        if (!opts.quiet) addMsg("user", text);
        if (local === "mic_off") setMic(false);
        else if (local === "voice_off") setVoice(false);
        else {
          setVoice(true);
          say("I'm back! I'll talk you through it.", "happy");
        }
        return;
      }
      var key = normText(text);
      if (msg && key && key === msg.key) return; // the recogniser repeated what Pulse is still answering
      var unsafe = looksUnsafe(text);
      var gate = gateLeft();
      if (opts.quiet) addNote(opts.note || text);
      else addMsg("user", text, via === "voice" ? "You · voice" : null);
      if (via === "voice") {
        // instant "I heard you" acknowledgement while the reply is on its way
        beep(880, 0.06);
        setTimeout(function () {
          beep(1320, 0.07);
        }, 75);
      }
      if (gate > 0 && !unsafe) {
        // every refused request counts against this network's IP: run the core commands here, hold the rest
        if (!runLocal(gateCommand(text))) notePaused(gate, true);
        return;
      }
      if (speaker.speaking) speaker.stop();
      if (gate > 0 || chatPauseLeft() > 0) {
        await sendPlain(text, via, unsafe);
        return;
      }
      abortMessage(); // one AI message at a time: the newest wins ...
      var mine = { ctrl: window.AbortController ? new AbortController() : null, key: key };
      if (!unsafe) msg = mine; // ... but words that may describe a health problem are never cut short by a newer one
      busy(1);
      var plain = false;
      try {
        var res = await aiRequest(urls.message, {
          json: { text: text, via: via === "voice" ? "voice" : "text", state: stateFor() },
          feature: "live_coach",
          timeout: 12000,
          signal: mine.ctrl ? mine.ctrl.signal : undefined,
          safety: unsafe,
        });
        if (msg === mine) msg = null;
        busy(-1);
        applyReply(res);
      } catch (err) {
        if (msg === mine) msg = null;
        busy(-1);
        if (err && err.code === "cooldown_local") plain = true; // AI got paused page-wide meanwhile
        else if (!(err && err.name === "AbortError" && err.reason !== "timeout")) handleError(err, "message");
        // (a newer message or the page closing aborted this one: nothing to say)
      }
      if (plain) await sendPlain(text, via, unsafe);
    }

    function intensity(word, note) {
      var t = Date.now();
      if (t - lastIntensity < 1500) return; // one step per 1.5 s, however fast the button is tapped
      lastIntensity = t;
      sendMessage(word, "text", { quiet: true, note: note });
    }

    function applyServer(res, ctx) {
      if (!res) return;
      ctx = ctx || {};
      var acts = res.actions || [];
      var safetyAct = null;
      acts.forEach(function (a) {
        if (a && (a.type === "safety_stop" || a.type === "suspended")) safetyAct = a;
      });
      var reply = res.reply || res.cue || "";
      if (reply) st.lastReply = reply;
      if (res.session) session = res.session;
      if (reply && !ctx.silent && (ctx.chat || safetyAct || ctx.event)) addMsg(safetyAct ? "alert" : "coach", reply, ctx.event ? null : metaFor(res));
      if (reply && !ctx.silent) say(reply, res.emotion, { high: !!(ctx.chat || safetyAct), alert: !!safetyAct });
      else if (res.emotion && !ctx.silent) setEmotion(res.emotion);
      acts.forEach(function (a) {
        handleAction(a || {});
      });
      if (!ctx.silent) showMemory(res);
      if (res.source === "upsell" && el.upsell) el.upsell.classList.remove("hidden");
      if (res.session) {
        if (res.session.status === "stopped_for_safety") enterSafety(safetyAct || {});
        else if (res.session.ended) enterDone();
      }
      render();
    }

    /** "Remember this?" / "Forgotten - Undo" cards (54-memory.js) under the transcript: never saved without a tap. */
    function showMemory(res) {
      if (!IP.memory || !el.log || !res || !res.memory || !res.memory.length) return;
      if (el.logEmpty) {
        el.logEmpty.remove();
        el.logEmpty = null;
      }
      IP.memory.render(el.log, res.memory);
      while (el.log.children.length > 80) el.log.removeChild(el.log.firstChild);
      el.log.scrollTop = el.log.scrollHeight;
    }

    function handleAction(a) {
      switch (a.type) {
        case "pause":
          pauseLocal();
          break;
        case "resume":
          resumeLocal();
          break;
        case "next":
          st.set = 1;
          if (st.begun) setPhase("ready", 6);
          break;
        case "replace_exercise":
          st.set = 1;
          if (st.phase === "paused") {
            st.prev = "ready";
            st.acc = 0;
            st.dur = 6;
          } else if (st.begun) setPhase("ready", 6);
          break;
        case "set_done":
          if (st.phase === "work") completeSet();
          else if (st.phase === "ready" || st.phase === "rest") startWork();
          break;
        case "quit":
        case "completed":
        case "ended":
          enterDone();
          break;
        case "safety_stop":
          enterSafety(a);
          break;
        case "suspended":
          enterSafety({ label: "Your gym activities are paused for your safety", suspended: true, emergency: a.emergency });
          break;
        case "mic_off":
          setMic(false);
          break;
        case "voice_off":
          setVoice(false);
          break;
        case "voice_on":
          setVoice(true);
          break;
        case "upsell":
          if (el.upsell) el.upsell.classList.remove("hidden");
          break;
      }
    }

    function handleError(err, ctx) {
      var status = err && err.status;
      var data = (err && err.data) || {};
      if (status === 401) {
        toast("Signed out", "Please sign in again to keep training.", "warning");
        return;
      }
      if (status === 403) {
        if (data.suspended) enterSafety({ label: "Your gym activities are paused for your safety", suspended: true });
        toast("Not available", data.detail || (err && err.message) || "", "warning");
        return;
      }
      if (status === 404) {
        toast("Session not found", "This session no longer exists.", "warning");
        return;
      }
      if (ctx !== "message" && ctx !== "discomfort") return;
      var wait = err && err.retryAfter > 0 ? err.retryAfter : 0;
      var text = status === 429
        ? "One moment - I'm catching my breath. Try again in " + (wait ? spokenWait(wait) : "a few seconds") + "."
        : "Sorry, I didn't catch that in time - say next, pause or easier.";
      addMsg("coach", text, "Pulse · offline");
      say(text, "calm", { high: true });
    }

    /* ---------------------------------------------------------- discomfort panel */
    var disc = { part: null, sev: "moderate" };
    function openPanel() {
      if (!el.panel || st.phase === "safety" || st.phase === "done") return;
      el.panel.hidden = false;
      var b = btn("discomfort");
      if (b) b.setAttribute("aria-expanded", "true");
      speaker.stop();
      if (pauseLocal()) sendEvent("paused", null, { silent: true });
      say("Okay - tell me where you feel it.", "empathetic", { high: true });
      var first = el.panel.querySelector("[data-part]");
      if (first) first.focus({ preventScroll: true });
      if (el.panel.scrollIntoView) el.panel.scrollIntoView({ behavior: "smooth", block: "nearest" });
    }
    function closePanel() {
      if (!el.panel) return;
      el.panel.hidden = true;
      var b = btn("discomfort");
      if (b) b.setAttribute("aria-expanded", "false");
    }
    function paintDisc() {
      root.querySelectorAll("[data-part]").forEach(function (c) {
        c.setAttribute("aria-pressed", c.getAttribute("data-part") === disc.part ? "true" : "false");
      });
      root.querySelectorAll("[data-sev]").forEach(function (c) {
        c.setAttribute("aria-pressed", c.getAttribute("data-sev") === disc.sev ? "true" : "false");
      });
      var serious = disc.sev === "severe" || disc.part === "chest" || disc.part === "head";
      if (el.warn) el.warn.classList.toggle("hidden", !serious);
      var r = btn("report");
      if (r) r.disabled = !disc.part;
    }
    async function reportDiscomfort() {
      if (!disc.part) return;
      var r = btn("report");
      if (r) r.disabled = true;
      var note = el.note ? el.note.value.trim().slice(0, 300) : "";
      addMsg("user", "I feel " + disc.sev + " discomfort in my " + (disc.part === "other" ? "body" : disc.part) + (note ? " - " + note : ""));
      busy(1);
      try {
        // a safety flow: never refused by a local AI cooldown, never aborted when the page hides (IP.ai safety)
        var res = await aiRequest(urls.discomfort, {
          json: { body_part: disc.part, severity: disc.sev, note: note || null, state: stateFor() },
          feature: "live_coach",
          safety: true,
        });
        busy(-1);
        closePanel();
        applyServer(res, { chat: true });
        disc.part = null;
        disc.sev = "moderate";
        if (el.note) el.note.value = "";
        paintDisc();
      } catch (err) {
        busy(-1);
        if (r) r.disabled = false;
        var serious = disc.sev === "severe" || disc.part === "chest" || disc.part === "head";
        if (serious) {
          var num = (cfg.emergency && cfg.emergency.number) || "112";
          st.lastReply = "Please stop now and rest. If it's severe or getting worse, call " + num + " right away.";
          say(st.lastReply, "concerned", { high: true, alert: true });
          enterSafety({ label: disc.part + " discomfort", emergency: cfg.emergency });
        } else handleError(err, "discomfort");
      }
    }

    /* ---------------------------------------------------------- end + wake lock */
    async function endSession() {
      if (st.phase === "done" || st.phase === "safety") return;
      if (!window.confirm("End this workout now?")) return;
      speaker.stop();
      try {
        var res = await IP.api(urls.end, { method: "POST", json: { reason: "user_ended" } });
        applyServer(res, { chat: true });
      } catch (err) {
        handleError(err, "end");
      }
      enterDone();
    }

    var wakeLock = null;
    function requestWakeLock() {
      try {
        if (!navigator.wakeLock || !navigator.wakeLock.request || wakeLock) return;
        navigator.wakeLock.request("screen").then(function (l) {
          wakeLock = l;
          l.addEventListener("release", function () {
            wakeLock = null;
          });
        }).catch(function () {});
      } catch (e) {}
    }
    function releaseWakeLock() {
      if (wakeLock) {
        try {
          wakeLock.release();
        } catch (e) {}
        wakeLock = null;
      }
    }

    /* ---------------------------------------------------------- wiring */
    root.addEventListener("click", function (e) {
      var chip = e.target.closest && e.target.closest("[data-part],[data-sev]");
      if (chip && root.contains(chip)) {
        if (chip.hasAttribute("data-part")) disc.part = chip.getAttribute("data-part");
        else disc.sev = chip.getAttribute("data-sev");
        paintDisc();
        return;
      }
      var b = e.target.closest && e.target.closest("[data-act]");
      if (!b || !root.contains(b) || b.disabled) return;
      switch (b.getAttribute("data-act")) {
        case "begin":
          begin();
          break;
        case "toggle":
          if (!st.begun) begin();
          else togglePause();
          break;
        case "next":
          skip();
          break;
        case "end":
          endSession();
          break;
        case "mic":
          setMic(!st.micOn);
          break;
        case "voice":
          setVoice(!st.voiceOn);
          break;
        case "setdone":
          primaryAction();
          break;
        case "discomfort":
          if (el.panel && !el.panel.hidden) closePanel();
          else openPanel();
          break;
        case "cancel-discomfort":
          closePanel();
          break;
        case "report":
          reportDiscomfort();
          break;
        case "easier":
          intensity("easier", "Intensity ↓ easier");
          break;
        case "harder":
          intensity("harder", "Intensity ↑ harder");
          break;
        case "im-ok":
          if (el.okNote) el.okNote.classList.remove("hidden");
          b.disabled = true;
          say("I'm glad you feel better. Please keep resting - your gym team will check in before you train again.", "empathetic", { high: true });
          break;
      }
    });

    var ptt = btn("ptt");
    if (ptt) {
      var pttDown = function (e) {
        if (e && e.button > 0) return;
        if (e && e.preventDefault) e.preventDefault();
        if (ptt.disabled) return;
        speaker.unlock();
        speaker.stop();
        ptt.setAttribute("data-active", "1");
        if (!rec) startRec("ptt");
      };
      var pttUp = function () {
        if (ptt.getAttribute("data-active") !== "1") return;
        ptt.removeAttribute("data-active");
        if (rec && recMode === "ptt") {
          try {
            rec.stop();
          } catch (x) {}
        }
      };
      ptt.addEventListener("pointerdown", pttDown);
      ptt.addEventListener("pointerup", pttUp);
      ptt.addEventListener("pointerleave", pttUp);
      ptt.addEventListener("pointercancel", pttUp);
      ptt.addEventListener("contextmenu", function (e) {
        e.preventDefault();
      });
      ptt.addEventListener("keydown", function (e) {
        if ((e.key === " " || e.key === "Enter") && !e.repeat) {
          e.preventDefault();
          pttDown();
        }
      });
      ptt.addEventListener("keyup", function (e) {
        if (e.key === " " || e.key === "Enter") {
          e.preventDefault();
          pttUp();
        }
      });
    }

    if (el.chatForm) {
      el.chatForm.setAttribute("data-no-busy", "");
      el.chatForm.addEventListener("submit", function (e) {
        e.preventDefault();
        var t = el.chatInput.value.trim();
        if (!t) return;
        el.chatInput.value = "";
        speaker.unlock();
        sendMessage(t, "text");
      });
    }

    document.addEventListener("keydown", function (e) {
      if (e.defaultPrevented || e.metaKey || e.ctrlKey || e.altKey) return;
      var t = e.target;
      if (t && t.closest && t.closest("input, textarea, select, button, a, summary, [contenteditable]")) return;
      var k = (e.key || "").toLowerCase();
      if (k === " " || k === "spacebar") {
        e.preventDefault();
        if (!st.begun) begin();
        else togglePause();
      } else if (k === "m") setMic(!st.micOn);
      else if (k === "n") skip();
      else if (k === "v") setVoice(!st.voiceOn);
    });

    document.addEventListener("visibilitychange", function () {
      if (!document.hidden && st.begun && (ACTIVE.indexOf(st.phase) >= 0 || st.phase === "paused")) requestWakeLock();
    });
    if (IP.ai && IP.ai.onCooldown) {
      IP.ai.onCooldown(function (info) {
        // HTTP 429 without a code on the message endpoint: the network's rate limit, not an AI refusal (a refused
        // reply handed to IP.ai never is, whatever its code)
        if (info && !noting && !info.ended && !info.code && info.feature === "live_coach" && info.seconds > 0) {
          gateUntil = Math.max(gateUntil, Date.now() + info.seconds * 1000);
        }
        paintChat(); // a pause started or ended - here or in any other AI widget of this page
      });
    }
    document.addEventListener("submit", function (e) {
      if (!e.defaultPrevented && isLogoutForm(e.target)) loggingOut = true; // after data-confirm had its say
    });
    window.addEventListener("pageshow", function (e) {
      if (e && e.persisted) loggingOut = false;
    });
    window.addEventListener("pagehide", function () {
      stopRec();
      speaker.stop(); // also drops the voice line being downloaded
      abortMessage(); // IP.ai (01-ai.js) has already aborted this page's AI requests - safety reports excepted
      // a workout left mid-set is paused on the server too - unless the page goes because of a sign-out: the session
      // cookie is gone by then, and a request failing CSRF counts against the gym's IP
      if (!loggingOut && st.begun && pauseLocal()) {
        IP.api(urls.event, { method: "POST", json: { type: "paused", state: stateFor() }, keepalive: true }).catch(function () {});
      }
    });

    if (!SR) {
      root.querySelectorAll("[data-mic-unsupported]").forEach(function (n) {
        n.classList.remove("hidden");
      });
      var note = root.querySelector("[data-mic-note]");
      if (note) note.textContent = "Voice input isn't supported in this browser - type instead. Pulse can still talk to you.";
    }

    // initial state (before Begin the mic toggle only records the preference - recognition starts on Begin)
    setVoice(st.voiceOn, true);
    setMic(prefs.mic !== false && !!SR, true);
    if (session.stopped_for_safety) {
      st.lastReply = el.safetyText ? el.safetyText.textContent : "";
      enterSafety({});
    } else if (session.ended) {
      enterDone();
    } else {
      render();
      face().setEmotion("happy");
    }
    setInterval(tick, 250);

    // diagnostics for tests
    IP.liveCoach = {
      state: st,
      session: function () {
        return session;
      },
      chat: function () {
        return { paused: chatPauseLeft(), gate: gateLeft(), inFlight: !!msg, pending: pending };
      },
    };
  });

  IP.liveCoachUtil = { isEcho: isEcho, localCommand: localCommand, fmt: fmt, normText: normText, looksUnsafe: looksUnsafe,
    gateCommand: gateCommand, spokenWait: spokenWait, isLogoutForm: isLogoutForm };
})();

;
/* 56-coach-face.js */
/* Live coach face (WP-K): a Memoji-style animated avatar drawn on <canvas data-component="coach-face">.
 * Emotion presets drive brows, eyes, cheeks, mouth and head tilt through critically-damped springs; idle
 * blinking, breathing and glances keep it alive; the mouth follows a lip-sync value (0..1) with rounder shapes
 * for O/U vowels. Respects prefers-reduced-motion and pauses while hidden.
 *   IP.coachFace.setEmotion(name)       neutral|happy|excited|encouraging|calm|concerned|empathetic|proud|focused
 *   IP.coachFace.setMouth(value, round) lip-sync 0..1 (+ optional roundness 0..1)
 *   IP.coachFace.setSpeaking(bool)      IP.coachFace.setListening(bool)      IP.coachFace.setThinking(bool)
 */
(function () {
  "use strict";
  var IP = window.IP;
  if (!IP) return;

  var EMOTIONS = {
    neutral: { browY: 0, browTilt: 0, eyeOpen: 1, squint: 0.06, smile: 0.32, mouthW: 1, mouthOpen: 0, blush: 0.25, tilt: 0 },
    happy: { browY: -2, browTilt: -0.05, eyeOpen: 0.96, squint: 0.36, smile: 0.88, mouthW: 1.08, mouthOpen: 0.12, blush: 0.5, tilt: -0.035 },
    excited: { browY: -6, browTilt: -0.1, eyeOpen: 1.12, squint: 0.16, smile: 1, mouthW: 1.14, mouthOpen: 0.32, blush: 0.62, tilt: 0.045 },
    encouraging: { browY: -3, browTilt: -0.06, eyeOpen: 1.02, squint: 0.26, smile: 0.72, mouthW: 1.05, mouthOpen: 0.07, blush: 0.42, tilt: 0.05 },
    calm: { browY: 1, browTilt: 0.03, eyeOpen: 0.8, squint: 0.14, smile: 0.42, mouthW: 0.96, mouthOpen: 0, blush: 0.3, tilt: -0.02 },
    concerned: { browY: -3, browTilt: 0.4, eyeOpen: 1.06, squint: 0, smile: -0.38, mouthW: 0.88, mouthOpen: 0.05, blush: 0.06, tilt: 0.065 },
    empathetic: { browY: -2, browTilt: 0.28, eyeOpen: 0.9, squint: 0.12, smile: 0.12, mouthW: 0.94, mouthOpen: 0, blush: 0.34, tilt: 0.085 },
    proud: { browY: -4, browTilt: -0.08, eyeOpen: 0.9, squint: 0.44, smile: 0.96, mouthW: 1.12, mouthOpen: 0.16, blush: 0.56, tilt: -0.06 },
    focused: { browY: 3, browTilt: -0.22, eyeOpen: 0.9, squint: 0.2, smile: 0.12, mouthW: 0.94, mouthOpen: 0, blush: 0.2, tilt: 0 },
  };
  var KEYS = Object.keys(EMOTIONS.neutral);
  var shared = { emotion: "neutral", mouth: 0, round: 0, speaking: false, listening: false, thinking: false, poke: 0 };
  var faces = [];

  function clamp(v, lo, hi) {
    return v < lo ? lo : v > hi ? hi : v;
  }

  IP.coachFace = {
    setEmotion: function (name) {
      shared.emotion = EMOTIONS[name] ? name : "neutral";
    },
    setMouth: function (value, round) {
      shared.mouth = clamp(+value || 0, 0, 1);
      if (round != null) shared.round = clamp(+round || 0, 0, 1);
    },
    setSpeaking: function (on) {
      shared.speaking = !!on;
      if (!on) {
        shared.mouth = 0;
        shared.round = 0;
      }
    },
    setListening: function (on) {
      shared.listening = !!on;
    },
    setThinking: function (on) {
      shared.thinking = !!on;
    },
    emotion: function () {
      return shared.emotion;
    },
    emotions: KEYS.length ? Object.keys(EMOTIONS) : [],
  };

  /* ---------------------------------------------------------------- drawing (virtual 300 x 300 canvas) */
  var SKIN_HI = "#ffe3cc";
  var SKIN = "#f7c49e";
  var SKIN_LO = "#e3a079";
  var HAIR_HI = "#5a3b26";
  var HAIR = "#36231a";
  var HAIR_LO = "#1f140e";
  var LIP = "#c85a6a";

  function headPath(ctx) {
    ctx.beginPath();
    ctx.moveTo(150, 44);
    ctx.bezierCurveTo(206, 44, 236, 86, 236, 138);
    ctx.bezierCurveTo(236, 190, 204, 232, 150, 234);
    ctx.bezierCurveTo(96, 232, 64, 190, 64, 138);
    ctx.bezierCurveTo(64, 86, 94, 44, 150, 44);
    ctx.closePath();
  }

  function radial(ctx, x0, y0, r0, x1, y1, r1, stops) {
    var g = ctx.createRadialGradient(x0, y0, r0, x1, y1, r1);
    for (var i = 0; i < stops.length; i++) g.addColorStop(stops[i][0], stops[i][1]);
    return g;
  }

  function linear(ctx, x0, y0, x1, y1, stops) {
    var g = ctx.createLinearGradient(x0, y0, x1, y1);
    for (var i = 0; i < stops.length; i++) g.addColorStop(stops[i][0], stops[i][1]);
    return g;
  }

  function drawTorso(ctx, p) {
    ctx.save();
    ctx.translate(0, p.bob * 0.45);
    ctx.beginPath();
    ctx.moveTo(26, 304);
    ctx.bezierCurveTo(32, 270, 78, 252, 122, 248);
    ctx.lineTo(178, 248);
    ctx.bezierCurveTo(222, 252, 268, 270, 274, 304);
    ctx.closePath();
    ctx.fillStyle = linear(ctx, 40, 250, 260, 304, [[0, "#3d9bff"], [0.55, "#0a84ff"], [1, "#5e5ce6"]]);
    ctx.fill();
    // shoulder highlight + zip line
    ctx.globalAlpha = 0.25;
    ctx.fillStyle = radial(ctx, 96, 262, 4, 96, 262, 70, [[0, "#ffffff"], [1, "rgba(255,255,255,0)"]]);
    ctx.fill();
    ctx.globalAlpha = 0.35;
    ctx.strokeStyle = "#ffffff";
    ctx.lineWidth = 2;
    ctx.beginPath();
    ctx.moveTo(150, 262);
    ctx.lineTo(150, 304);
    ctx.stroke();
    ctx.globalAlpha = 1;
    // neck
    ctx.beginPath();
    ctx.moveTo(124, 206);
    ctx.lineTo(176, 206);
    ctx.lineTo(178, 252);
    ctx.quadraticCurveTo(150, 266, 122, 252);
    ctx.closePath();
    ctx.fillStyle = linear(ctx, 0, 206, 0, 262, [[0, "#d9906a"], [0.45, SKIN_LO], [1, SKIN]]);
    ctx.fill();
    // collar
    ctx.beginPath();
    ctx.moveTo(116, 250);
    ctx.quadraticCurveTo(150, 272, 184, 250);
    ctx.lineWidth = 7;
    ctx.lineCap = "round";
    ctx.strokeStyle = "#0866d8";
    ctx.stroke();
    ctx.restore();
  }

  function drawEar(ctx, x, flip) {
    ctx.save();
    ctx.translate(x, 146);
    ctx.scale(flip, 1);
    ctx.beginPath();
    ctx.ellipse(0, 0, 13, 20, -0.12, 0, Math.PI * 2);
    ctx.fillStyle = radial(ctx, -3, -4, 2, 0, 0, 22, [[0, SKIN_HI], [0.6, SKIN], [1, SKIN_LO]]);
    ctx.fill();
    ctx.beginPath();
    ctx.ellipse(2, 1, 6, 11, -0.12, 0, Math.PI * 2);
    ctx.fillStyle = "rgba(196, 116, 84, 0.35)";
    ctx.fill();
    ctx.restore();
  }

  function drawHair(ctx) {
    ctx.beginPath();
    ctx.moveTo(63, 136);
    ctx.bezierCurveTo(54, 74, 96, 30, 152, 30);
    ctx.bezierCurveTo(208, 30, 248, 70, 237, 136);
    ctx.bezierCurveTo(231, 114, 222, 104, 208, 99);
    ctx.bezierCurveTo(178, 108, 132, 108, 104, 97);
    ctx.bezierCurveTo(90, 104, 74, 114, 63, 136);
    ctx.closePath();
    ctx.fillStyle = linear(ctx, 80, 30, 220, 130, [[0, HAIR_HI], [0.5, HAIR], [1, HAIR_LO]]);
    ctx.fill();
    // glossy streak
    ctx.save();
    ctx.globalAlpha = 0.18;
    ctx.beginPath();
    ctx.moveTo(108, 48);
    ctx.quadraticCurveTo(150, 34, 196, 52);
    ctx.lineWidth = 6;
    ctx.lineCap = "round";
    ctx.strokeStyle = "#a07850";
    ctx.stroke();
    ctx.restore();
  }

  function drawHeadband(ctx) {
    ctx.save();
    headPath(ctx);
    ctx.clip();
    ctx.beginPath();
    ctx.moveTo(52, 104);
    ctx.quadraticCurveTo(150, 60, 248, 104);
    ctx.lineTo(248, 121);
    ctx.quadraticCurveTo(150, 77, 52, 121);
    ctx.closePath();
    ctx.fillStyle = linear(ctx, 60, 0, 240, 0, [[0, "#0a84ff"], [0.6, "#5e5ce6"], [1, "#bf5af2"]]);
    ctx.fill();
    ctx.globalAlpha = 0.35;
    ctx.beginPath();
    ctx.moveTo(60, 106);
    ctx.quadraticCurveTo(150, 65, 240, 106);
    ctx.lineWidth = 2;
    ctx.strokeStyle = "#ffffff";
    ctx.stroke();
    ctx.restore();
  }

  function drawBrow(ctx, cx, y, tilt, side, lift) {
    var inner = cx - side * 17;
    var outer = cx + side * 17;
    var yi = y - tilt * 11 - lift;
    var yo = y + 2 - lift * 0.4;
    ctx.beginPath();
    ctx.moveTo(inner, yi);
    ctx.quadraticCurveTo(cx, Math.min(yi, yo) - 6, outer, yo);
    ctx.lineWidth = 7.5;
    ctx.lineCap = "round";
    ctx.strokeStyle = HAIR;
    ctx.stroke();
  }

  function drawEye(ctx, cx, cy, open, squint, lookX, lookY, happyArc) {
    var rx = 15;
    var top = 17 * open;
    var bottom = 15 * open * (1 - squint * 0.9);
    if (open < 0.14) {
      // closed: a soft lid line (curves up when smiling)
      ctx.beginPath();
      ctx.moveTo(cx - rx, cy);
      ctx.quadraticCurveTo(cx, cy + (happyArc ? -7 : 4), cx + rx, cy);
      ctx.lineWidth = 3;
      ctx.lineCap = "round";
      ctx.strokeStyle = "#2a1a12";
      ctx.stroke();
      return;
    }
    ctx.save();
    ctx.beginPath();
    ctx.moveTo(cx - rx, cy);
    ctx.bezierCurveTo(cx - rx, cy - top * 1.32, cx + rx, cy - top * 1.32, cx + rx, cy);
    ctx.bezierCurveTo(cx + rx, cy + bottom * 1.3, cx - rx, cy + bottom * 1.3, cx - rx, cy);
    ctx.closePath();
    ctx.fillStyle = radial(ctx, cx, cy - 4, 2, cx, cy, 20, [[0, "#ffffff"], [1, "#e6e9ef"]]);
    ctx.fill();
    ctx.clip();
    var ix = cx + lookX;
    var iy = cy + lookY + 1;
    ctx.beginPath();
    ctx.arc(ix, iy, 10.5, 0, Math.PI * 2);
    ctx.fillStyle = radial(ctx, ix, iy, 1, ix, iy, 11, [[0, "#9a6234"], [0.55, "#6a3f1f"], [0.9, "#3a2010"], [1, "#24130a"]]);
    ctx.fill();
    ctx.beginPath();
    ctx.arc(ix, iy, 5.2, 0, Math.PI * 2);
    ctx.fillStyle = "#0c0806";
    ctx.fill();
    ctx.beginPath();
    ctx.arc(ix - 3.6, iy - 4.2, 3.3, 0, Math.PI * 2);
    ctx.fillStyle = "rgba(255,255,255,0.95)";
    ctx.fill();
    ctx.beginPath();
    ctx.arc(ix + 3.4, iy + 3.2, 1.5, 0, Math.PI * 2);
    ctx.fillStyle = "rgba(255,255,255,0.7)";
    ctx.fill();
    // upper-lid shadow inside the eye
    ctx.fillStyle = "rgba(120, 70, 50, 0.18)";
    ctx.fillRect(cx - rx, cy - top * 1.4, rx * 2, 5);
    ctx.restore();
    // smiling eyes: the cheeks push the lower lid up
    if (squint > 0.2) {
      ctx.beginPath();
      ctx.moveTo(cx - rx + 2, cy + bottom * 1.02);
      ctx.quadraticCurveTo(cx, cy + bottom * 1.02 - 2 - squint * 4, cx + rx - 2, cy + bottom * 1.02);
      ctx.lineWidth = 2;
      ctx.lineCap = "round";
      ctx.strokeStyle = "rgba(170, 92, 62, " + (0.55 * squint).toFixed(3) + ")";
      ctx.stroke();
    }
    // lash line
    ctx.beginPath();
    ctx.moveTo(cx - rx - 1, cy + 0.5);
    ctx.bezierCurveTo(cx - rx, cy - top * 1.34, cx + rx, cy - top * 1.34, cx + rx + 1, cy + 0.5);
    ctx.lineWidth = 2.8;
    ctx.lineCap = "round";
    ctx.strokeStyle = "rgba(34, 20, 12, 0.92)";
    ctx.stroke();
  }

  function drawMouth(ctx, p) {
    var cx = 150;
    var cy = 198;
    var round = p.round;
    var open = clamp(p.mouthOpen + p.mouth * 0.95, 0, 1);
    var w = 46 * p.mouthW * (1 - 0.38 * round * Math.min(1, open * 3));
    var h = (1.5 + open * 25) * (1 + 0.45 * round);
    var corner = -p.smile * 7;
    var lx = cx - w / 2;
    var rx = cx + w / 2;
    var upY = cy - 2.5 - p.smile * 1.2 - h * 0.18;
    var dnY = cy + 3 + p.smile * 9 + h;
    if (h < 4.2) {
      ctx.beginPath();
      ctx.moveTo(lx, cy + corner);
      ctx.quadraticCurveTo(cx, cy + p.smile * 11, rx, cy + corner);
      ctx.lineWidth = 4.2;
      ctx.lineCap = "round";
      ctx.strokeStyle = LIP;
      ctx.stroke();
      // tiny dimples
      ctx.globalAlpha = 0.35 * clamp(p.smile, 0, 1);
      ctx.beginPath();
      ctx.arc(lx - 3, cy + corner - 1.5, 2, 0, Math.PI * 2);
      ctx.arc(rx + 3, cy + corner - 1.5, 2, 0, Math.PI * 2);
      ctx.fillStyle = "#c46a52";
      ctx.fill();
      ctx.globalAlpha = 1;
      return;
    }
    ctx.save();
    ctx.beginPath();
    ctx.moveTo(lx, cy + corner);
    ctx.quadraticCurveTo(cx, upY, rx, cy + corner);
    ctx.quadraticCurveTo(cx, dnY, lx, cy + corner);
    ctx.closePath();
    ctx.fillStyle = linear(ctx, 0, upY, 0, dnY, [[0, "#3a0d14"], [1, "#6b1d2a"]]);
    ctx.fill();
    ctx.clip();
    // teeth
    ctx.fillStyle = "#fbfbfd";
    ctx.fillRect(lx, upY - 6, w, Math.min(9, h * 0.4) + 6);
    // tongue
    ctx.beginPath();
    ctx.ellipse(cx, dnY - 3, w * 0.3, Math.max(3, h * 0.32), 0, 0, Math.PI * 2);
    ctx.fillStyle = "#e46d7f";
    ctx.fill();
    ctx.restore();
    ctx.beginPath();
    ctx.moveTo(lx, cy + corner);
    ctx.quadraticCurveTo(cx, upY, rx, cy + corner);
    ctx.quadraticCurveTo(cx, dnY, lx, cy + corner);
    ctx.closePath();
    ctx.lineWidth = 3;
    ctx.lineJoin = "round";
    ctx.strokeStyle = LIP;
    ctx.stroke();
  }

  function draw(ctx, p, t) {
    ctx.clearRect(0, 0, 300, 300);
    drawTorso(ctx, p);
    ctx.save();
    // head group: bob + tilt around the neck
    ctx.translate(150 + p.swayX, 226 + p.bob);
    ctx.rotate(p.tilt + p.sway + p.nod);
    ctx.translate(-150, -226);
    drawEar(ctx, 64, -1);
    drawEar(ctx, 236, 1);
    // head with a soft drop shadow
    ctx.save();
    ctx.shadowColor = "rgba(40, 30, 90, 0.28)";
    ctx.shadowBlur = 26;
    ctx.shadowOffsetY = 10;
    headPath(ctx);
    ctx.fillStyle = radial(ctx, 122, 104, 8, 150, 140, 170, [[0, SKIN_HI], [0.42, SKIN], [1, SKIN_LO]]);
    ctx.fill();
    ctx.restore();
    // rim light + chin shade
    ctx.save();
    headPath(ctx);
    ctx.clip();
    ctx.fillStyle = radial(ctx, 98, 82, 4, 98, 82, 96, [[0, "rgba(255,255,255,0.32)"], [1, "rgba(255,255,255,0)"]]);
    ctx.fillRect(40, 40, 220, 200);
    ctx.fillStyle = radial(ctx, 150, 250, 20, 150, 250, 92, [[0, "rgba(160, 80, 50, 0.22)"], [1, "rgba(160, 80, 50, 0)"]]);
    ctx.fillRect(40, 140, 220, 100);
    ctx.restore();
    drawHair(ctx);
    drawHeadband(ctx);
    // cheeks
    var blush = clamp(p.blush, 0, 1);
    if (blush > 0.01) {
      ctx.fillStyle = radial(ctx, 98, 180, 2, 98, 180, 24, [[0, "rgba(255, 105, 120, " + (0.42 * blush).toFixed(3) + ")"], [1, "rgba(255,105,120,0)"]]);
      ctx.fillRect(70, 152, 56, 56);
      ctx.fillStyle = radial(ctx, 202, 180, 2, 202, 180, 24, [[0, "rgba(255, 105, 120, " + (0.42 * blush).toFixed(3) + ")"], [1, "rgba(255,105,120,0)"]]);
      ctx.fillRect(174, 152, 56, 56);
    }
    var lift = p.speakLift;
    drawBrow(ctx, 112, 113 + p.browY, p.browTilt, 1, lift);
    drawBrow(ctx, 188, 113 + p.browY, p.browTilt, -1, lift);
    var open = clamp(p.eyeOpen * (1 - p.blink), 0, 1.25);
    var happyArc = p.smile > 0.6;
    drawEye(ctx, 112, 143, open, p.squint, p.lookX, p.lookY, happyArc);
    drawEye(ctx, 188, 143, open, p.squint, p.lookX, p.lookY, happyArc);
    // nose
    ctx.beginPath();
    ctx.moveTo(142, 174);
    ctx.quadraticCurveTo(150, 181, 158, 174);
    ctx.lineWidth = 3;
    ctx.lineCap = "round";
    ctx.strokeStyle = "rgba(170, 92, 62, 0.42)";
    ctx.stroke();
    ctx.beginPath();
    ctx.ellipse(149, 164, 5, 7, 0, 0, Math.PI * 2);
    ctx.fillStyle = "rgba(255, 255, 255, 0.18)";
    ctx.fill();
    drawMouth(ctx, p);
    ctx.restore();
    // let the shoulders fade into the stage instead of ending in a hard edge
    ctx.save();
    ctx.globalCompositeOperation = "destination-out";
    ctx.fillStyle = linear(ctx, 0, 258, 0, 300, [[0, "rgba(0,0,0,0)"], [1, "rgba(0,0,0,1)"]]);
    ctx.fillRect(0, 258, 300, 44);
    ctx.restore();
  }

  /* ---------------------------------------------------------------- animation */
  function makeState(emotion) {
    var e = EMOTIONS[emotion] || EMOTIONS.neutral;
    var s = { vel: {}, blink: 0, lookX: 0, lookY: 0, mouth: 0, round: 0, speakLift: 0 };
    KEYS.forEach(function (k) {
      s[k] = e[k];
      s.vel[k] = 0;
    });
    return s;
  }

  function step(face, now) {
    var s = face.state;
    var dt = Math.min(0.05, Math.max(0.001, (now - face.last) / 1000));
    face.last = now;
    var reduced = face.reduced;
    var target = EMOTIONS[shared.emotion] || EMOTIONS.neutral;
    var k = 70;
    var c = 2 * Math.sqrt(k);
    KEYS.forEach(function (key) {
      var goal = target[key];
      if (key === "eyeOpen" && shared.listening) goal += 0.06;
      if (key === "tilt" && shared.listening) goal += 0.04;
      if (key === "browY" && shared.thinking) goal -= 2;
      var a = k * (goal - s[key]) - c * s.vel[key];
      s.vel[key] += a * dt;
      s[key] += s.vel[key] * dt;
    });
    // lip-sync (fast attack, softer release)
    var m = shared.speaking ? shared.mouth : 0;
    s.mouth += (m - s.mouth) * (m > s.mouth ? 0.55 : 0.22);
    s.round += ((shared.speaking ? shared.round : 0) - s.round) * 0.25;
    s.speakLift += ((shared.speaking ? Math.max(0, shared.mouth - 0.55) * 6 : 0) - s.speakLift) * 0.12;
    // blinking every 3-6 s (sometimes a double blink)
    if (now >= face.nextBlink) {
      face.blinkStart = now;
      face.nextBlink = now + (reduced ? 5000 : 3000) + Math.random() * 3000;
      if (Math.random() < 0.18) face.doubleAt = now + 260;
    }
    if (face.doubleAt && now >= face.doubleAt) {
      face.blinkStart = now;
      face.doubleAt = 0;
    }
    var bp = (now - face.blinkStart) / 150;
    s.blink = bp >= 0 && bp <= 1 ? Math.sin(bp * Math.PI) : 0;
    // glances
    if (!reduced && now >= face.nextGlance) {
      face.nextGlance = now + 2000 + Math.random() * 3200;
      face.lookTarget = [Math.random() * 7 - 3.5, Math.random() * 4 - 2];
    }
    var lt = face.lookTarget;
    if (shared.listening || reduced) lt = [0, 0.5];
    if (shared.thinking) lt = [-4.5, -4];
    s.lookX += (lt[0] - s.lookX) * 0.08;
    s.lookY += (lt[1] - s.lookY) * 0.08;
    var t = now / 1000;
    var p = {
      bob: reduced ? 0 : Math.sin(t * 1.55) * 2.2,
      sway: reduced ? 0 : Math.sin(t * 0.63) * 0.014,
      swayX: reduced ? 0 : Math.sin(t * 0.41) * 1.2,
      nod: reduced || !shared.speaking ? 0 : Math.sin(t * 7.3) * s.mouth * 0.022,
      blink: s.blink,
      lookX: s.lookX,
      lookY: s.lookY,
      mouth: s.mouth,
      round: s.round,
      speakLift: s.speakLift,
    };
    KEYS.forEach(function (key) {
      p[key] = s[key];
    });
    if (face.poke && now - face.poke < 900) {
      p.smile = Math.max(p.smile, 0.95);
      p.squint = Math.max(p.squint, 0.4);
      p.blush = Math.max(p.blush, 0.6);
    }
    return p;
  }

  function resize(face) {
    var el = face.canvas;
    var w = el.clientWidth || 300;
    var h = el.clientHeight || w;
    var dpr = Math.min(window.devicePixelRatio || 1, 2.5);
    var bw = Math.max(1, Math.round(w * dpr));
    var bh = Math.max(1, Math.round(h * dpr));
    if (el.width !== bw || el.height !== bh) {
      el.width = bw;
      el.height = bh;
    }
    var scale = (Math.min(w, h) / 300) * dpr;
    face.transform = [scale, (bw - 300 * scale) / 2, (bh - 300 * scale) / 2];
  }

  function paint(face, now) {
    var p = step(face, now);
    var tr = face.transform;
    face.ctx.setTransform(1, 0, 0, 1, 0, 0);
    face.ctx.clearRect(0, 0, face.canvas.width, face.canvas.height);
    face.ctx.setTransform(tr[0], 0, 0, tr[0], tr[1], tr[2]);
    draw(face.ctx, p, now);
  }

  function frame(now) {
    faces.forEach(function (face) {
      if (face.visible && !document.hidden) paint(face, now);
    });
    rafId = faces.length ? window.requestAnimationFrame(frame) : 0;
  }
  var rafId = 0;

  IP.register("coach-face", function (canvas) {
    var ctx = canvas.getContext && canvas.getContext("2d");
    if (!ctx) return;
    var initial = canvas.getAttribute("data-emotion");
    if (initial && EMOTIONS[initial]) shared.emotion = initial;
    var mq = window.matchMedia ? window.matchMedia("(prefers-reduced-motion: reduce)") : null;
    var now = window.performance ? performance.now() : Date.now();
    var face = {
      canvas: canvas,
      ctx: ctx,
      state: makeState(shared.emotion),
      last: now,
      nextBlink: now + 1200,
      blinkStart: -1000,
      doubleAt: 0,
      nextGlance: now + 1500,
      lookTarget: [0, 0],
      reduced: !!(mq && mq.matches),
      visible: true,
      poke: 0,
      transform: [1, 0, 0],
    };
    if (mq && mq.addEventListener) {
      mq.addEventListener("change", function () {
        face.reduced = mq.matches;
      });
    }
    function refit() {
      resize(face);
      paint(face, window.performance ? performance.now() : Date.now());
    }
    refit(); // first frame right away (RAF may be throttled in background tabs)
    if (window.ResizeObserver) new ResizeObserver(refit).observe(canvas);
    else window.addEventListener("resize", refit);
    if (window.IntersectionObserver) {
      new IntersectionObserver(function (entries) {
        face.visible = entries[entries.length - 1].isIntersecting;
      }).observe(canvas);
    }
    canvas.addEventListener("pointerdown", function () {
      face.poke = window.performance ? performance.now() : Date.now();
      face.blinkStart = face.poke;
    });
    faces.push(face);
    if (!rafId) rafId = window.requestAnimationFrame(frame);
  });

  // test/diagnostic hook (no-op in production paths)
  IP.coachFace._draw = draw;
  IP.coachFace._emotionTable = EMOTIONS;
})();

;
/* 60-chat.js */
/* WP-F: live chat (iMessage-like bubbles), conversation list, AI member intake and small form helpers.
 *
 * Components (markup declares data-component="..."):
 *   chat-thread  - one conversation: live messages over the WebSocket (IP.on("chat.message")), optimistic sends
 *                  via IP.api, auto-scroll, day separators, typing indicator, mark-read on focus and a 5 s polling
 *                  fallback while the WebSocket is down. Trainer safety: a staff / owner message the server flags
 *                  (409 code "safety_check") comes back into the composer with the warning (Edit / Send anyway ->
 *                  {"confirm_safety": true}); a member's red-flag message shows the emergency guidance at once.
 *   chat-list    - the conversation sidebar: live previews, reordering and unread badges; "Load more
 *                  conversations" appends the next keyset window from GET /api/chat/threads (the link itself
 *                  still works without JavaScript).
 *   intake-ai    - owner registration: free text -> POST /owner/customers/intake -> fills the form, highlights what
 *                  is still missing / invalid, shows the AI provider + disclaimer.
 *   member-form  - registration form: marks the guardian section as required for under-18s.
 *   char-count   - live "123 / 4,000" counter under a textarea (data-min / data-max).
 * User data is only ever rendered with IP.h / textContent (never innerHTML).
 */
(function () {
  "use strict";
  var IP = window.IP;
  if (!IP) return;

  // ------------------------------------------------------------------ helpers
  function myId() {
    return parseInt(document.body.getAttribute("data-user-id") || "0", 10) || 0;
  }

  function pad(n) {
    return (n < 10 ? "0" : "") + n;
  }

  function dayKey(d) {
    return d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate());
  }

  function dayLabel(d) {
    var today = new Date();
    today.setHours(0, 0, 0, 0);
    var that = new Date(d.getTime());
    that.setHours(0, 0, 0, 0);
    var diff = Math.round((today.getTime() - that.getTime()) / 86400000);
    if (diff === 0) return "Today";
    if (diff === 1) return "Yesterday";
    try {
      if (diff > 1 && diff < 7) return d.toLocaleDateString([], { weekday: "long" });
      var opts = { weekday: "short", day: "numeric", month: "short" };
      if (d.getFullYear() !== new Date().getFullYear()) opts.year = "numeric";
      return d.toLocaleDateString([], opts);
    } catch (e) {
      return dayKey(d);
    }
  }

  function initials(name) {
    var parts = String(name || "").trim().split(/\s+/).filter(Boolean);
    if (!parts.length) return "?";
    if (parts.length === 1) return parts[0].slice(0, 2).toUpperCase();
    return (parts[0][0] + parts[parts.length - 1][0]).toUpperCase();
  }

  function face(url, name) {
    var span = IP.h("span", { class: "avatar size-7 text-[10px]" });
    if (url) span.appendChild(IP.h("img", { src: url, alt: "", loading: "lazy" }));
    else span.textContent = initials(name);
    return span;
  }

  function shortTime(iso) {
    if (!iso) return "";
    var d = new Date(iso);
    if (isNaN(d.getTime())) return "";
    var now = new Date();
    if (dayKey(d) === dayKey(now)) return IP.fmtTime(iso);
    return dayLabel(d);
  }

  /** One message row - identical markup to the server macro chat/_macros.html:message_row. */
  function buildRow(msg, opts) {
    if (msg.sender_role === "system") {
      var safety = String(msg.body || "").indexOf("Safety first:") === 0;
      return IP.h(
        "div",
        { class: "chat-row chat-row-gap justify-center", "data-msg-id": msg.id || "", "data-ts": msg.created_at || "" },
        IP.h("p", { class: "chat-bubble chat-bubble-system" + (safety ? " chat-bubble-safety" : ""), text: msg.body })
      );
    }
    var mine = !!opts.mine;
    var row = IP.h("div", {
      class: "chat-row" + (mine ? " chat-row-mine" : "") + (opts.first ? " chat-row-gap" : ""),
      "data-msg-id": msg.id || "",
      "data-sender": msg.sender_id || "",
      "data-role": msg.sender_role || "",
      "data-ts": msg.created_at || "",
    });
    if (!mine) {
      var slot = IP.h("span", { class: "chat-avatar-slot" });
      if (opts.first) slot.appendChild(face(msg.sender_photo, msg.sender_name));
      row.appendChild(slot);
    }
    var bubble = IP.h("div", {
      class: "chat-bubble " + (mine ? "chat-bubble-mine" : "chat-bubble-theirs") + (opts.pending ? " chat-bubble-pending" : ""),
    });
    if (!mine && opts.first) {
      bubble.appendChild(
        IP.h("p", { class: "chat-sender", text: (msg.sender_name || "") + (msg.sender_label ? " · " + msg.sender_label : "") })
      );
    }
    bubble.appendChild(IP.h("p", { class: "chat-body", text: msg.body }));
    bubble.appendChild(
      IP.h(
        "p",
        { class: "chat-meta" },
        IP.h("time", { datetime: msg.created_at || "", text: opts.pending ? "Sending…" : IP.fmtTime(msg.created_at) })
      )
    );
    row.appendChild(bubble);
    return row;
  }

  // ------------------------------------------------------------------ chat-thread
  IP.register("chat-thread", function (root) {
    var threadId = parseInt(root.getAttribute("data-thread-id"), 10);
    var me = parseInt(root.getAttribute("data-me") || "0", 10) || myId();
    var role = root.getAttribute("data-role") || "customer";
    var api = root.getAttribute("data-api");
    var readUrl = root.getAttribute("data-read-url");
    var readOnly = root.getAttribute("data-readonly") === "1";
    var lastId = parseInt(root.getAttribute("data-last-id") || "0", 10) || 0;
    var firstId = parseInt(root.getAttribute("data-first-id") || "0", 10) || 0;
    var hasMore = root.getAttribute("data-has-more") === "1";
    var scroller = root.querySelector("[data-chat-scroll]");
    var log = root.querySelector("[data-chat-log]");
    var end = root.querySelector("[data-chat-end]");
    var form = root.querySelector("[data-chat-form]");
    var input = root.querySelector("[data-chat-input]");
    var sendBtn = root.querySelector("[data-chat-send]");
    var typingBox = root.querySelector("[data-chat-typing]");
    var typingName = root.querySelector("[data-chat-typing-name]");
    var jump = root.querySelector("[data-chat-jump]");
    var statusEl = root.querySelector("[data-chat-status]");
    var moreLink = root.querySelector("[data-chat-more]");
    var warnBox = root.querySelector("[data-chat-warning]");
    var alertBox = root.querySelector("[data-chat-alert]");
    var olderPage = /[?&]before=/.test(location.search);
    if (!threadId || !scroller || !log || !end) return;

    var seen = {};
    var unseen = false;
    var typingTimer = null;
    var lastTypingSent = 0;
    var readTimer = null;
    var polling = false;
    var loadingOlder = false;

    log.querySelectorAll("[data-msg-id]").forEach(function (row) {
      var id = parseInt(row.getAttribute("data-msg-id"), 10);
      if (id) seen[id] = true;
    });

    function say(text) {
      if (statusEl) statusEl.textContent = text;
    }

    function nearBottom() {
      return scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < 140;
    }

    function scrollToBottom(smooth) {
      try {
        scroller.scrollTo({ top: scroller.scrollHeight, behavior: smooth ? "smooth" : "auto" });
      } catch (e) {
        scroller.scrollTop = scroller.scrollHeight;
      }
    }

    function lastRow() {
      var rows = log.querySelectorAll(".chat-row");
      return rows.length ? rows[rows.length - 1] : null;
    }

    function lastDay() {
      var days = log.querySelectorAll("[data-chat-day]");
      return days.length ? days[days.length - 1].getAttribute("data-chat-day") : null;
    }

    function append(msg, opts) {
      opts = opts || {};
      var empty = log.querySelector("[data-chat-empty]");
      if (empty) empty.remove();
      var d = msg.created_at ? new Date(msg.created_at) : new Date();
      if (isNaN(d.getTime())) d = new Date();
      var key = dayKey(d);
      var newDay = key !== lastDay();
      if (newDay) log.insertBefore(IP.h("div", { class: "chat-day", "data-chat-day": key, text: dayLabel(d) }), end);
      var prev = lastRow();
      var mine = opts.mine != null ? opts.mine : msg.sender_id === me;
      var first =
        newDay ||
        !prev ||
        prev.getAttribute("data-sender") !== String(msg.sender_id || "") ||
        prev.getAttribute("data-role") !== String(msg.sender_role || "");
      var row = buildRow(msg, { mine: mine, first: first, pending: !!opts.pending });
      log.insertBefore(row, end);
      return row;
    }

    function confirmRow(row, msg) {
      if (!row || !msg) return;
      row.removeAttribute("data-pending");
      seen[msg.id] = true;
      row.setAttribute("data-msg-id", msg.id);
      row.setAttribute("data-ts", msg.created_at || "");
      var bubble = row.querySelector(".chat-bubble");
      if (bubble) bubble.classList.remove("chat-bubble-pending", "chat-bubble-failed");
      var t = row.querySelector("time");
      if (t) {
        t.setAttribute("datetime", msg.created_at || "");
        t.textContent = IP.fmtTime(msg.created_at);
      }
      var retry = row.querySelector("[data-chat-retry]");
      if (retry) retry.remove();
      lastId = Math.max(lastId, msg.id || 0);
    }

    function pendingFor(body) {
      var rows = log.querySelectorAll('.chat-row[data-pending="1"]');
      for (var i = 0; i < rows.length; i++) if (rows[i].__body === body) return rows[i];
      return null;
    }

    function showJump() {
      if (jump) jump.hidden = false;
    }

    function hideJump() {
      if (jump) jump.hidden = true;
    }

    function receive(msg) {
      if (!msg || !msg.id || seen[msg.id]) return;
      if (msg.sender_id === me) {
        var pend = pendingFor(msg.body);
        if (pend) {
          confirmRow(pend, msg);
          return;
        }
      }
      var stick = nearBottom();
      seen[msg.id] = true;
      append(msg);
      lastId = Math.max(lastId, msg.id);
      if (msg.sender_id !== me) {
        hideTyping();
        unseen = true;
        say("New message from " + (msg.sender_name || "the other person"));
        if (stick) {
          scrollToBottom(true);
          markReadSoon();
        } else {
          showJump();
        }
      } else {
        scrollToBottom(true);
      }
    }

    function markRead() {
      if (!unseen || !readUrl || document.visibilityState === "hidden") return;
      unseen = false;
      IP.api(readUrl, { method: "POST", json: {} })
        .then(function () {
          IP.emit("chat.read", { thread_id: threadId });
        })
        .catch(function () {
          unseen = true;
        });
    }

    function markReadSoon() {
      clearTimeout(readTimer);
      readTimer = setTimeout(function () {
        if (document.hasFocus()) markRead();
      }, 600);
    }

    // ---- typing indicator
    function hideTyping() {
      if (typingBox) typingBox.hidden = true;
      clearTimeout(typingTimer);
    }

    IP.on("chat.typing", function (evt) {
      if (!evt || evt.thread_id !== threadId || evt.user_id === me || !typingBox) return;
      if (typingName) typingName.textContent = (evt.name || "Someone") + " is typing…";
      var stick = nearBottom();
      typingBox.hidden = false;
      if (stick) scrollToBottom(false);
      clearTimeout(typingTimer);
      typingTimer = setTimeout(hideTyping, 4000);
    });

    IP.on("chat.message", function (evt) {
      if (evt && evt.thread_id === threadId) receive(evt.message);
    });

    IP.on("chat.status", function (evt) {
      if (evt && evt.thread_id === threadId) {
        say("This conversation was " + (evt.status === "closed" ? "closed" : "reopened") + ".");
        setTimeout(function () {
          location.reload();
        }, 900);
      }
    });

    // ---- polling fallback (WebSocket down) + catch-up after reconnect
    async function poll() {
      if (polling || olderPage) return;
      polling = true;
      try {
        var data = await IP.api(api + "?after=" + lastId + "&limit=100");
        (data.items || []).forEach(receive);
        if (data.read_only && !readOnly) location.reload();
      } catch (e) {
        /* offline - try again on the next tick */
      } finally {
        polling = false;
      }
    }

    setInterval(function () {
      if (!IP.ws.connected && document.visibilityState !== "hidden") poll();
    }, 5000);
    IP.on("ws.open", poll);

    // ---- read state
    window.addEventListener("focus", markRead);
    document.addEventListener("visibilitychange", function () {
      if (document.visibilityState === "visible") markRead();
    });
    scroller.addEventListener("scroll", function () {
      if (nearBottom()) {
        hideJump();
        if (unseen && document.hasFocus()) markRead();
      }
    });
    if (jump) {
      jump.addEventListener("click", function () {
        hideJump();
        scrollToBottom(true);
        markRead();
      });
    }

    // ---- older messages without a page load
    if (moreLink && hasMore && firstId && !olderPage) {
      moreLink.addEventListener("click", async function (e) {
        e.preventDefault();
        if (loadingOlder) return;
        loadingOlder = true;
        moreLink.setAttribute("aria-busy", "true");
        try {
          var data = await IP.api(api + "?before=" + firstId + "&limit=50");
          var items = (data.items || []).filter(function (m) {
            return !seen[m.id];
          });
          if (items.length) {
            var oldHeight = scroller.scrollHeight;
            var frag = document.createDocumentFragment();
            var prevKey = null;
            var prevSender = null;
            items.forEach(function (m) {
              seen[m.id] = true;
              var d = new Date(m.created_at);
              var key = dayKey(d);
              var newDay = key !== prevKey;
              if (newDay) frag.appendChild(IP.h("div", { class: "chat-day", "data-chat-day": key, text: dayLabel(d) }));
              var sk = String(m.sender_id || "") + "|" + m.sender_role;
              frag.appendChild(buildRow(m, { mine: m.sender_id === me, first: newDay || sk !== prevSender }));
              prevKey = key;
              prevSender = sk;
            });
            var firstDay = log.querySelector("[data-chat-day]");
            if (firstDay && firstDay.getAttribute("data-chat-day") === prevKey) firstDay.remove();
            var anchor = moreLink.parentElement;
            anchor.parentNode.insertBefore(frag, anchor.nextSibling);
            firstId = items[0].id;
            scroller.scrollTop += scroller.scrollHeight - oldHeight;
          }
          if (!data.has_more) anchor_remove();
        } catch (err) {
          window.location.href = moreLink.getAttribute("href");
        } finally {
          loadingOlder = false;
          moreLink.removeAttribute("aria-busy");
        }
      });
    }

    function anchor_remove() {
      if (moreLink && moreLink.parentElement) moreLink.parentElement.remove();
    }

    // ---- composer
    function autosize() {
      if (!input) return;
      input.style.height = "auto";
      input.style.height = Math.min(input.scrollHeight, 160) + "px";
    }

    function failRow(row, text, err) {
      var bubble = row.querySelector(".chat-bubble");
      if (bubble) {
        bubble.classList.remove("chat-bubble-pending");
        bubble.classList.add("chat-bubble-failed");
      }
      var t = row.querySelector("time");
      if (t) t.textContent = "Not sent";
      if (!row.querySelector("[data-chat-retry]") && bubble) {
        var retry = IP.h("button", {
          type: "button",
          class: "mt-1 text-[11px] font-semibold underline underline-offset-2",
          "data-chat-retry": "1",
          text: "Tap to retry",
        });
        retry.addEventListener("click", function () {
          row.remove();
          send(text);
        });
        bubble.appendChild(retry);
      }
      var msg = (err && err.message) || "Message not sent.";
      say(msg);
      if (IP.island) IP.island.notify({ title: "Message not sent", body: msg, tone: "danger", timeout: 5000 });
      if (err && err.status === 409 && err.code !== "safety_check") {
        setTimeout(function () {
          location.reload();
        }, 1500);
      }
    }

    // ---- trainer safety: the send-time warning (staff / owner) and the member's red-flag guidance
    function hideWarning() {
      if (!warnBox) return;
      warnBox.hidden = true;
      while (warnBox.firstChild) warnBox.removeChild(warnBox.firstChild);
    }

    function showWarning(text, warning) {
      if (!warnBox) return;
      hideWarning();
      warning = warning || {};
      warnBox.appendChild(IP.h("p", { class: "font-semibold", text: warning.title || "Check this before you send it" }));
      warnBox.appendChild(IP.h("p", { class: "mt-1 text-sm", text: warning.message || "" }));
      var lines = warning.lines || [];
      if (lines.length) {
        var ul = IP.h("ul", { class: "mt-1.5 list-disc space-y-0.5 pl-5 text-xs" });
        lines.forEach(function (line) {
          ul.appendChild(IP.h("li", { text: line }));
        });
        warnBox.appendChild(ul);
      }
      var edit = IP.h("button", { type: "button", class: "btn btn-secondary btn-sm", text: "Edit message" });
      var anyway = IP.h("button", { type: "button", class: "btn btn-danger btn-sm", text: "Send anyway" });
      edit.addEventListener("click", function () {
        hideWarning();
        if (input) input.focus();
      });
      anyway.addEventListener("click", function () {
        if (anyway.disabled) return;
        anyway.disabled = true; // one click = one message (double-submit guard)
        hideWarning();
        if (input && input.value === text) {
          input.value = "";
          autosize();
        }
        send(text, true);
      });
      warnBox.appendChild(IP.h("div", { class: "mt-2.5 flex flex-wrap gap-2" }, edit, anyway));
      warnBox.hidden = false;
      say(warning.message || "Check this message before sending it.");
    }

    function showAlert(safety) {
      if (!alertBox || !safety) return;
      while (alertBox.firstChild) alertBox.removeChild(alertBox.firstChild);
      alertBox.appendChild(IP.h("p", { class: "font-semibold", text: safety.title || "Your safety comes first" }));
      alertBox.appendChild(IP.h("p", { class: "mt-1", text: safety.message || "" }));
      if (safety.tel) {
        alertBox.appendChild(
          IP.h("a", { href: safety.tel, class: "btn btn-danger btn-sm mt-2.5", text: "Call " + (safety.number || "emergency services") })
        );
      }
      alertBox.hidden = false;
      say(safety.message || "");
    }

    async function send(text, confirmSafety) {
      var row = append(
        { id: 0, body: text, sender_id: me, sender_role: role, created_at: new Date().toISOString() },
        { mine: true, pending: true }
      );
      row.setAttribute("data-pending", "1");
      row.__body = text;
      hideJump();
      scrollToBottom(true);
      var payload = { body: text };
      if (confirmSafety) payload.confirm_safety = true;
      try {
        var res = await IP.api(api, { method: "POST", json: payload });
        confirmRow(row, res && res.message);
        say("Message sent");
        if (res && res.safety) showAlert(res.safety);
      } catch (err) {
        if (err && err.code === "safety_check") {
          // nothing was sent: the text goes back into the composer with the warning
          row.remove();
          if (input && !input.value.trim()) {
            input.value = text;
            autosize();
          }
          showWarning(text, err.data && err.data.warning);
          return;
        }
        failRow(row, text, err);
      }
    }

    if (form && input && !readOnly) {
      form.addEventListener("submit", function (e) {
        e.preventDefault();
        var text = input.value.replace(/\s+$/, "");
        if (!text.trim()) {
          input.focus();
          return;
        }
        if (text.length > 4000) {
          say("Messages can be at most 4,000 characters.");
          if (IP.island) IP.island.notify({ title: "Message too long", body: "Keep it under 4,000 characters.", tone: "warning" });
          return;
        }
        input.value = "";
        autosize();
        hideWarning();
        send(text);
        input.focus();
      });
      input.addEventListener("keydown", function (e) {
        if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
          e.preventDefault();
          if (typeof form.requestSubmit === "function") form.requestSubmit();
          else form.dispatchEvent(new Event("submit", { cancelable: true }));
        }
      });
      input.addEventListener("input", function () {
        autosize();
        var now = Date.now();
        if (IP.ws.connected && input.value.trim() && now - lastTypingSent > 2000) {
          lastTypingSent = now;
          IP.ws.send({ type: "chat.typing", thread_id: threadId });
        }
      });
      if (sendBtn) sendBtn.disabled = false;
      autosize();
    }

    // ---- first paint
    if (!olderPage) {
      scrollToBottom(false);
      requestAnimationFrame(function () {
        scrollToBottom(false);
      });
      if (input && !readOnly && window.matchMedia("(min-width: 1024px)").matches) input.focus({ preventScroll: true });
    }
    root.__chat = { receive: receive, poll: poll };
  });

  // ------------------------------------------------------------------ chat-list
  IP.register("chat-list", function (list) {
    var me = parseInt(list.getAttribute("data-me") || "0", 10) || myId();
    var active = parseInt(list.getAttribute("data-active") || "0", 10) || 0;
    var role = list.getAttribute("data-role") || "customer";

    function itemFor(id) {
      return list.querySelector('[data-thread-item="' + id + '"]');
    }

    function setBadge(a, n) {
      var badge = a.querySelector("[data-unread]");
      if (!badge) return;
      badge.textContent = n > 99 ? "99+" : String(n);
      badge.setAttribute("aria-label", n + " unread");
      badge.hidden = n <= 0;
      a.classList.toggle("chat-item-unread", n > 0);
    }

    IP.on("chat.message", function (evt) {
      if (!evt || !evt.message) return;
      var msg = evt.message;
      var a = itemFor(evt.thread_id);
      if (!a) {
        var link = IP.h(
          "a",
          { href: "/chat/" + evt.thread_id, class: "chat-item chat-item-unread", "data-thread-item": evt.thread_id },
          IP.h("span", { class: "avatar size-11 text-sm" }, initials(msg.sender_name)),
          IP.h(
            "span",
            { class: "min-w-0 flex-1" },
            IP.h(
              "span",
              { class: "flex items-baseline justify-between gap-2" },
              IP.h("span", { class: "chat-item-name truncate text-[15px] font-medium", text: msg.sender_name || "New message" }),
              IP.h("time", { class: "shrink-0 text-[11px] tabular-nums muted", "data-time": "1", text: shortTime(msg.created_at) })
            ),
            IP.h(
              "span",
              { class: "mt-0.5 flex items-center gap-2" },
              IP.h("span", { class: "chat-item-preview min-w-0 flex-1 truncate text-[13px] muted", "data-preview": "1", text: evt.preview || msg.body }),
              IP.h("span", { class: "chat-unread-badge shrink-0", "data-unread": "1", text: "1" })
            )
          )
        );
        list.insertBefore(IP.h("li", {}, link), list.firstChild);
        return;
      }
      var preview = a.querySelector("[data-preview]");
      if (preview) preview.textContent = evt.preview || msg.body;
      var time = a.querySelector("[data-time]");
      if (time) {
        time.textContent = shortTime(msg.created_at);
        time.setAttribute("datetime", msg.created_at || "");
      }
      var li = a.closest("li");
      if (li && li.parentNode === list && list.firstElementChild !== li) list.insertBefore(li, list.firstElementChild);
      if (evt.thread_id !== active && msg.sender_id !== me) {
        // same rules as the server: members count customer_unread, staff count staff_unread,
        // owners only count messages to the admin team
        var n;
        if (role === "customer") n = evt.customer_unread;
        else if (role === "staff") n = evt.staff_unread;
        else n = a.getAttribute("data-team") === "1" ? evt.staff_unread : 0;
        if (typeof n === "number") setBadge(a, n);
      }
    });

    IP.on("chat.read", function (evt) {
      var a = evt && itemFor(evt.thread_id);
      if (a) setBadge(a, 0);
    });

    // ---- "Load more conversations": append the next window in place (same markup as chat/_macros.html:thread_item)
    var box = list.parentNode;
    var more = box && box.querySelector("[data-chat-more-threads]");
    var loadingMore = false;

    function icon(name) {
      var tpl = box.querySelector('template[data-chat-icon="' + name + '"]');
      var node = tpl && tpl.content ? tpl.content.firstElementChild || tpl.content.firstChild : null;
      return node ? node.cloneNode(true) : null;
    }

    function threadItem(t) {
      var avatar = IP.h("span", { class: "avatar size-11 text-sm" });
      if (t.photo_url) avatar.appendChild(IP.h("img", { src: t.photo_url, alt: t.name || "", loading: "lazy" }));
      else avatar.textContent = initials(t.name);
      var badge = null;
      if (t.deleted) badge = IP.h("span", { class: "badge badge-zinc shrink-0" }, icon("lock"), "Read-only");
      else if (t.closed) badge = IP.h("span", { class: "badge badge-zinc shrink-0", text: "Closed" });
      var unread = t.unread | 0;
      var counter = IP.h("span", {
        class: "chat-unread-badge shrink-0",
        "data-unread": "1",
        "aria-label": unread + " unread",
        hidden: unread ? null : true,
        text: unread > 99 ? "99+" : String(unread),
      });
      var link = IP.h(
        "a",
        { href: t.href, class: "chat-item" + (unread ? " chat-item-unread" : ""), "data-thread-item": t.id, "data-team": t.team ? "1" : "0" },
        IP.h("span", { class: "relative shrink-0" }, avatar, t.team ? icon("team") : null),
        IP.h(
          "span",
          { class: "min-w-0 flex-1" },
          IP.h(
            "span",
            { class: "flex items-baseline justify-between gap-2" },
            IP.h("span", { class: "chat-item-name truncate text-[15px] font-medium", text: t.name || "" }),
            IP.h(
              "span",
              { class: "flex shrink-0 items-center gap-1.5" },
              t.safety ? icon("safety") : null,
              IP.h("time", { class: "text-[11px] tabular-nums muted", datetime: t.last_at || "", "data-time": "1", text: t.when || "" })
            )
          ),
          IP.h("span", { class: "block truncate text-xs muted", text: t.subtitle || "" }),
          IP.h(
            "span",
            { class: "mt-0.5 flex items-center gap-2" },
            IP.h("span", { class: "chat-item-preview min-w-0 flex-1 truncate text-[13px] muted", "data-preview": "1", text: t.preview || "" }),
            badge,
            counter
          )
        )
      );
      return IP.h("li", {}, link);
    }

    if (more && more.getAttribute("data-api")) {
      more.addEventListener("click", function (e) {
        var url = more.getAttribute("data-api");
        if (!url) return; // follow the link
        e.preventDefault();
        if (loadingMore) return;
        loadingMore = true;
        more.setAttribute("aria-busy", "true");
        IP.api(url)
          .then(function (data) {
            (data.items || []).forEach(function (t) {
              if (t && t.id && !itemFor(t.id)) list.appendChild(threadItem(t));
            });
            if (data.has_more && data.more_api && data.more_url) {
              more.setAttribute("data-api", data.more_api);
              more.setAttribute("href", data.more_url);
            } else {
              var wrap = more.closest("[data-chat-more-wrap]") || more;
              wrap.remove();
              more = null;
            }
          })
          .catch(function () {
            if (more) location.href = more.getAttribute("href"); // the server-rendered window still works
          })
          .then(function () {
            loadingMore = false;
            if (more) more.removeAttribute("aria-busy");
          });
      });
    }
  });

  // ------------------------------------------------------------------ toast for chat messages on other pages
  IP.on("chat.message", function (evt) {
    if (!evt || !evt.message || evt.message.sender_id === myId()) return;
    var path = location.pathname;
    if (path === "/chat/" + evt.thread_id || (path === "/chat" && document.querySelector('[data-component~="chat-list"]'))) return;
    if (IP.island) {
      IP.island.notify({
        title: evt.message.sender_name || "New message",
        body: evt.preview || evt.message.body,
        tone: "info",
        link: "/chat/" + evt.thread_id,
      });
    }
  });

  // ------------------------------------------------------------------ char-count
  IP.register("char-count", function (field) {
    var max = parseInt(field.getAttribute("data-max") || field.getAttribute("maxlength") || "0", 10) || 0;
    var min = parseInt(field.getAttribute("data-min") || "0", 10) || 0;
    if (!max) return;
    var out = IP.h("p", { class: "mt-1 text-right text-[11px] tabular-nums muted", "aria-live": "polite" });
    field.insertAdjacentElement("afterend", out);
    function fmt(n) {
      try {
        return n.toLocaleString();
      } catch (e) {
        return String(n);
      }
    }
    function update() {
      var n = field.value.length;
      out.textContent = fmt(n) + " / " + fmt(max) + (min && n > 0 && n < min ? " · at least " + fmt(min) : "");
      out.classList.toggle("text-bad", n > max || (min > 0 && n > 0 && n < min));
      out.classList.toggle("muted", !(n > max || (min > 0 && n > 0 && n < min)));
    }
    field.addEventListener("input", update);
    update();
  });

  // ------------------------------------------------------------------ member-form (guardian required for minors)
  function ageFrom(value) {
    if (!value) return null;
    var m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value);
    if (!m) return null;
    var dob = new Date(+m[1], +m[2] - 1, +m[3]);
    var now = new Date();
    var age = now.getFullYear() - dob.getFullYear();
    if (now.getMonth() < dob.getMonth() || (now.getMonth() === dob.getMonth() && now.getDate() < dob.getDate())) age -= 1;
    return age;
  }

  function setMinor(form, minor) {
    var badge = form.querySelector("[data-minor-badge]");
    var section = form.querySelector("[data-guardian-section]");
    if (badge) badge.hidden = !minor;
    if (section) section.classList.toggle("ring-2", !!minor);
    if (section) section.classList.toggle("ring-violet/40", !!minor);
  }

  IP.register("member-form", function (form) {
    var dob = form.querySelector("[data-dob]");
    function check() {
      var age = ageFrom(dob && dob.value);
      setMinor(form, age !== null && age < 18);
    }
    if (dob) {
      dob.addEventListener("change", check);
      dob.addEventListener("input", check);
      check();
    }
    form.addEventListener("input", function (e) {
      if (e.target && e.target.classList) e.target.classList.remove("intake-missing");
    });
  });

  // ------------------------------------------------------------------ intake-ai
  IP.register("intake-ai", function (box) {
    /* The request goes through IP.ai (feature "intake", docs/ai-guard-and-queue.md section 7): a refusal (quota,
     * cooldown, too many at once, duplicate, spam) shows the server's own words, and the run button stays disabled -
     * counting the wait down - while IP.ai.cooldownLeft("intake") > 0. Leaving the page aborts the request (reason
     * "page-hide"): not an error, never shown. A full AI queue is answered by the automatic parser ("AI busy"). */
    var FEATURE = "intake";
    var SERVER_WORDS = { quota: true, cooldown: true, too_many: true, duplicate: true, spam: true };
    var url = box.getAttribute("data-url");
    var form = document.getElementById(box.getAttribute("data-form") || "");
    var min = parseInt(box.getAttribute("data-min") || "10", 10);
    var text = box.querySelector("[data-intake-text]");
    var run = box.querySelector("[data-intake-run]");
    var label = box.querySelector("[data-intake-label]");
    var result = box.querySelector("[data-intake-result]");
    var errorEl = box.querySelector("[data-intake-error]");
    if (!url || !form || !text || !run) return;
    var added = [];
    var idle = (label && label.textContent) || "Fill with AI";
    var running = false;
    var tick = null;

    function errorText(err) {
      var code = err && typeof err.code === "string" ? err.code : "";
      var data = err && err.data && typeof err.data === "object" ? err.data : null;
      var detail = data && typeof data.detail === "string" ? data.detail.trim() : "";
      if (SERVER_WORDS[code] && detail && detail.length <= 300) return detail;
      return IP.ai.describeError(err);
    }

    /** The server asked us to wait: keep the button disabled and say for how long ("Try again in 45 s"). */
    function paintWait() {
      var left = IP.ai.cooldownLeft(FEATURE);
      if (!running) {
        run.disabled = left > 0;
        if (label) label.textContent = left > 0 ? "Try again in " + IP.ai.formatWait(left) : idle;
      }
      if (left > 0 && !tick) tick = setInterval(paintWait, 1000);
      if (!(left > 0) && tick) {
        clearInterval(tick);
        tick = null;
      }
    }
    IP.ai.onCooldown(paintWait);

    function el(sel) {
      return box.querySelector(sel);
    }

    function fieldEl(name) {
      var f = form.elements.namedItem(name);
      if (f && f.length && !f.tagName) f = f[0];
      return f || null;
    }

    function clearMarks() {
      form.querySelectorAll(".intake-filled, .intake-missing").forEach(function (n) {
        n.classList.remove("intake-filled", "intake-missing");
      });
      added.forEach(function (n) {
        n.remove();
      });
      added = [];
    }

    function showError(msg) {
      if (!errorEl) return;
      errorEl.textContent = msg || "";
      errorEl.classList.toggle("hidden", !msg);
    }

    function fillList(sel, wrapSel, items, render) {
      var ul = el(sel);
      var wrap = el(wrapSel);
      if (!ul || !wrap) return;
      while (ul.firstChild) ul.removeChild(ul.firstChild);
      items.forEach(function (it) {
        ul.appendChild(render(it));
      });
      wrap.hidden = !items.length;
    }

    function apply(data) {
      clearMarks();
      var fields = data.fields || {};
      var filled = 0;
      Object.keys(fields).forEach(function (name) {
        var value = fields[name];
        var f = fieldEl(name);
        if (!f || value === null || value === undefined || value === "") return;
        if (f.tagName === "SELECT") {
          var ok = Array.prototype.some.call(f.options, function (o) {
            return o.value === String(value);
          });
          if (!ok) return;
        }
        f.value = String(value);
        f.classList.add("intake-filled");
        f.dispatchEvent(new Event("input", { bubbles: true }));
        f.dispatchEvent(new Event("change", { bubbles: true }));
        filled += 1;
      });
      var src = form.querySelector("[data-intake-source]");
      if (src) src.value = "owner_ai";

      var invalid = data.invalid || {};
      Object.keys(invalid).forEach(function (name) {
        var f = fieldEl(name);
        if (!f) return;
        f.classList.remove("intake-filled");
        f.classList.add("intake-missing");
        var p = IP.h("p", { class: "field-error", text: invalid[name] });
        f.insertAdjacentElement("afterend", p);
        added.push(p);
      });
      (data.missing || []).forEach(function (name) {
        var f = fieldEl(name);
        if (f) f.classList.add("intake-missing");
        if (name === "id_front") {
          var idBox = form.querySelector("[data-id-section] fieldset");
          if (idBox) idBox.classList.add("intake-missing");
        }
      });
      if (data.minor) setMinor(form, true);

      var provider = el("[data-intake-provider]");
      if (provider) {
        while (provider.firstChild) provider.removeChild(provider.firstChild);
        var state = data.ai_available ? "online" : data.busy ? "degraded" : "offline";
        provider.appendChild(IP.h("span", { class: "ai-dot", "data-state": state }));
        provider.appendChild(document.createTextNode(
          data.provider || (data.busy ? "Automatic parser (AI busy)" : "Automatic parser (AI unavailable)")
        ));
      }
      var summary = el("[data-intake-summary]");
      if (summary) {
        summary.textContent = filled
          ? "Filled " + filled + " field" + (filled === 1 ? "" : "s") + " - please review them in the form."
          : "Nothing usable was found - try adding the name, contact and health details.";
      }
      fillList("[data-intake-missing-list]", "[data-intake-missing]", data.missing_labels || [], function (t) {
        return IP.h("li", { class: "badge badge-amber", text: t });
      });
      var inv = Object.keys(invalid).map(function (k) {
        return invalid[k];
      });
      fillList("[data-intake-invalid-list]", "[data-intake-invalid]", inv, function (t) {
        return IP.h("li", { class: "text-[#d1281e] dark:text-[#ff6b61]", text: t });
      });
      fillList("[data-intake-dropped-list]", "[data-intake-dropped]", data.dropped || [], function (d) {
        return IP.h("li", { text: (d.field || "").replace(/_/g, " ") + ": " + (d.value || "") + " - " + (d.reason || "") });
      });
      var notes = el("[data-intake-notes]");
      if (notes) {
        while (notes.firstChild) notes.removeChild(notes.firstChild);
        (data.notes || []).forEach(function (n) {
          notes.appendChild(IP.h("li", { text: n }));
        });
      }
      var disc = el("[data-intake-disclaimer]");
      if (disc) disc.textContent = data.disclaimer || "";
      result.hidden = false;
      var target = form.querySelector(".intake-missing") || form;
      if (window.matchMedia("(max-width: 1023px)").matches) target.scrollIntoView({ behavior: "smooth", block: "center" });
      if (IP.island) {
        IP.island.notify({
          title: data.source === "ai" ? "Form filled by AI" : "Form filled automatically",
          body: (data.missing_labels || []).length ? "Still needed: " + data.missing_labels.join(", ") : "Review and save.",
          tone: data.source === "ai" ? "success" : "warning",
          timeout: 3600,
        });
      }
    }

    run.addEventListener("click", async function () {
      if (running) return;
      if (IP.ai.cooldownLeft(FEATURE) > 0) {
        paintWait(); // the button is disabled; a keyboard shortcut can still land here
        return;
      }
      var value = text.value.trim();
      showError("");
      if (value.length < min) {
        showError("Describe the member in at least " + min + " characters.");
        text.focus();
        return;
      }
      running = true;
      run.disabled = true;
      run.setAttribute("aria-busy", "true");
      if (label) label.textContent = "Reading your note…";
      var spin = IP.h("span", { class: "spinner", "aria-hidden": "true" });
      run.insertBefore(spin, run.firstChild);
      try {
        var data = await IP.ai.request(url, { json: { text: value }, feature: FEATURE });
        apply(data);
      } catch (err) {
        if (!(err && err.name === "AbortError" && err.reason === "page-hide")) showError(errorText(err));
      } finally {
        running = false;
        run.removeAttribute("aria-busy");
        spin.remove();
        paintWait(); // the label back - or the wait, when the server asked us to hold off
      }
    });
    text.addEventListener("keydown", function (e) {
      if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
        e.preventDefault();
        run.click();
      }
    });
    paintWait();
  });
})();

;
/* 70-superadmin.js */
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

;
/* 71-insights.js */
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

;
/* 80-shop.js */
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

;