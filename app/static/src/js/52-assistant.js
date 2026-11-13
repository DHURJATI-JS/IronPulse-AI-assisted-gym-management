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
