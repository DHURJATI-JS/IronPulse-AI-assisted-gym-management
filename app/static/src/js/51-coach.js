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
