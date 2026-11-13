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
