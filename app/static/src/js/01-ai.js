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
