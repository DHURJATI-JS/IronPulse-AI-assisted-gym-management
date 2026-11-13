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
