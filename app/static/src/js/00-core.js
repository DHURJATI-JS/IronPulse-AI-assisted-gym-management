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
