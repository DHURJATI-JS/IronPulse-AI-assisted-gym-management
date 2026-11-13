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
