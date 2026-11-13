/* WP-C2 finance & payroll: chart tooltips, live proof-verification status, proof dropzone.
 * Strict CSP: no inline handlers; DOM is built with IP.h / textContent only.
 */
(function () {
  "use strict";
  var IP = window.IP;
  if (!IP) return;

  /* ------------------------------------------------------------------ 12-month chart tooltip */
  IP.register("fin-chart", function (root) {
    var tip = root.querySelector("[data-tooltip]");
    var frame = tip ? tip.parentElement : null;
    var hits = root.querySelectorAll(".fin-hit");
    if (!tip || !frame || !hits.length) return;
    var active = null;

    function row(keyClass, label, value) {
      return IP.h("div", { class: "mt-1 flex items-center justify-between gap-4" },
        IP.h("span", { class: "flex items-center gap-1.5 opacity-75" },
          keyClass ? IP.h("span", { class: "fin-key " + keyClass, "aria-hidden": "true" }) : null, label),
        IP.h("strong", { class: "tabular-nums", text: value }));
    }

    function show(rect) {
      if (active && active !== rect) active.removeAttribute("data-active");
      active = rect;
      rect.setAttribute("data-active", "1");
      tip.textContent = "";
      tip.appendChild(IP.h("p", { class: "font-semibold", text: rect.getAttribute("data-label") || "" }));
      tip.appendChild(row("fin-key-in", "Income", rect.getAttribute("data-in") || ""));
      tip.appendChild(row("fin-key-out", "Outgoing", rect.getAttribute("data-out") || ""));
      tip.appendChild(row(null, "Net", rect.getAttribute("data-net") || ""));
      var fr = frame.getBoundingClientRect();
      var rr = rect.getBoundingClientRect();
      var x = rr.left - fr.left + rr.width / 2;
      var half = 96;
      x = Math.max(half, Math.min(fr.width - half, x));
      tip.style.left = x + "px";
      tip.style.top = Math.max(0, rr.top - fr.top + 8) + "px";
      tip.hidden = false;
    }

    function hide() {
      tip.hidden = true;
      if (active) active.removeAttribute("data-active");
      active = null;
    }

    hits.forEach(function (rect) {
      // the native <title> is the no-JS fallback; drop it so two tooltips never overlap
      var title = rect.querySelector("title");
      if (title) title.remove();
      rect.addEventListener("pointerenter", function () { show(rect); });
      rect.addEventListener("focus", function () { show(rect); });
      rect.addEventListener("blur", hide);
      rect.addEventListener("keydown", function (e) {
        var list = Array.prototype.slice.call(hits);
        var i = list.indexOf(rect);
        if (e.key === "ArrowRight" && i < list.length - 1) { e.preventDefault(); list[i + 1].focus(); }
        if (e.key === "ArrowLeft" && i > 0) { e.preventDefault(); list[i - 1].focus(); }
        if (e.key === "Escape") { hide(); rect.blur(); }
      });
    });
    root.addEventListener("pointerleave", hide);
  });

  /* ------------------------------------------------------------------ live status while a proof is verified */
  IP.register("payroll-status", function (el) {
    var url = el.getAttribute("data-url");
    var status = el.getAttribute("data-status");
    var explaining = el.getAttribute("data-explaining") === "1";
    if (!url || (status !== "proof_review" && !explaining)) return;
    var tries = 0;
    var stopped = false;

    function reload() {
      if (stopped) return;
      stopped = true;
      window.location.reload();
    }

    async function poll() {
      if (stopped) return;
      tries += 1;
      try {
        var data = await IP.api(url);
        if (data.status !== status || (explaining && !data.explaining)) return reload();
      } catch (err) {
        if (err && (err.status === 401 || err.status === 403 || err.status === 404)) return;
      }
      if (tries < 90) setTimeout(poll, tries < 10 ? 3000 : 6000);
    }

    setTimeout(poll, 2500);
    // a "payment verified / needs attention" notification for this statement refreshes immediately
    var base = url.replace(/\/status$/, "");
    IP.on("notification", function (evt) {
      var link = (evt && evt.link) || "";
      if (link === base || link.indexOf(base + "#") === 0 || link.indexOf(base + "?") === 0) reload();
    });
  });

  /* ------------------------------------------------------------------ "run payroll" for every gym, in the background */
  IP.register("payroll-run", function (el) {
    var id = el.getAttribute("data-task");
    var url = el.getAttribute("data-url");
    var bar = el.querySelector("progress");
    var text = el.querySelector("[data-progress-text]");
    if (!id) return;
    var tries = 0;
    var stopped = false;

    function finish() {
      if (stopped) return;
      stopped = true;
      window.location.reload(); // the statements of every gym (and the outcome) are on the page now
    }

    function show(progress, message) {
      var pct = Number(progress);
      if (bar && isFinite(pct)) bar.value = Math.max(0, Math.min(100, pct));
      if (text && typeof message === "string" && message) {
        text.textContent = message + " - statements appear below as each gym finishes.";
      }
    }

    // the job reports each gym it finishes over the socket ...
    IP.on("task.update", function (evt) {
      if (!evt || String(evt.task_id) !== id) return;
      if (evt.status === "running") show(evt.progress, evt.message);
      else if (evt.status === "done" || evt.status === "failed" || evt.status === "cancelled") finish();
    });

    // ... and a slow poll covers a quiet socket
    async function poll() {
      if (stopped || !url) return;
      tries += 1;
      try {
        var data = await IP.api(url);
        if (data.status === "running" || data.status === "queued" || data.status === "paused") show(data.progress, data.message);
        else return finish();
      } catch (err) {
        if (err && (err.status === 401 || err.status === 403 || err.status === 404)) return;
      }
      if (tries < 120) setTimeout(poll, tries < 10 ? 4000 : 10000);
    }

    setTimeout(poll, 4000);
  });

  /* ------------------------------------------------------------------ proof dropzone */
  IP.register("proof-upload", function (form) {
    var input = form.querySelector('input[type="file"]');
    var zone = form.querySelector("[data-dropzone]");
    var label = form.querySelector("[data-file-label]");
    if (!input || !zone || !label) return;
    var maxMb = parseInt(form.getAttribute("data-max-mb") || "8", 10) || 8;
    var preview = null;

    function human(bytes) {
      if (bytes < 1024 * 1024) return Math.max(1, Math.round(bytes / 1024)) + " KB";
      return (bytes / (1024 * 1024)).toFixed(1) + " MB";
    }

    function describe() {
      var f = input.files && input.files[0];
      if (preview) { URL.revokeObjectURL(preview.src); preview.remove(); preview = null; }
      zone.classList.remove("ring-2", "ring-bad/60");
      if (!f) { label.textContent = "Choose a file"; return; }
      var okType = /^image\//.test(f.type) || f.type === "application/pdf" || /\.(pdf|jpe?g|png|webp|heic)$/i.test(f.name);
      if (!okType) {
        label.textContent = "Please choose an image or a PDF";
        zone.classList.add("ring-2", "ring-bad/60");
        input.value = "";
        return;
      }
      if (f.size > maxMb * 1024 * 1024) {
        label.textContent = f.name + " is larger than " + maxMb + " MB";
        zone.classList.add("ring-2", "ring-bad/60");
        input.value = "";
        return;
      }
      label.textContent = f.name + " · " + human(f.size);
      if (/^image\//.test(f.type)) {
        preview = IP.h("img", { alt: "Selected proof preview", class: "mt-2 max-h-40 rounded-xl object-contain shadow-sm" });
        preview.src = URL.createObjectURL(f);
        zone.appendChild(preview);
      }
    }

    input.addEventListener("change", describe);
    ["dragenter", "dragover"].forEach(function (type) {
      zone.addEventListener(type, function (e) {
        e.preventDefault();
        zone.classList.add("border-accent", "bg-accent/[0.06]");
      });
    });
    ["dragleave", "drop"].forEach(function (type) {
      zone.addEventListener(type, function (e) {
        e.preventDefault();
        zone.classList.remove("border-accent", "bg-accent/[0.06]");
      });
    });
    zone.addEventListener("drop", function (e) {
      if (e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files.length) {
        input.files = e.dataTransfer.files;
        describe();
      }
    });
  });
})();
