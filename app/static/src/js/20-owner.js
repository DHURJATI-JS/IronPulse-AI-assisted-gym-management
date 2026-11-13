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
