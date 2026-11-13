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
