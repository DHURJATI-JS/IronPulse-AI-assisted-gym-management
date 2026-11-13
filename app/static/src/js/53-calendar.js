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
