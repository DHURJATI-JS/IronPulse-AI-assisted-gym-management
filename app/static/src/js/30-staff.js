/* WP-C1 staff & hiring enhancements (progressive: every form works without JavaScript).
 *   data-component="photo-preview"    live preview of the chosen staff photo
 *   data-component="currency-default" payroll country -> default currency (until the user edits it)
 *   data-component="staff-form"       trainer-only hints follow the selected role
 *   data-component="shift-form"       weekday presets + branch field only for offline shifts
 *   data-component="job-form"         "Remote" disables the location field
 */
(function () {
  "use strict";
  var IP = window.IP;
  if (!IP) return;

  // Staff portraits: fall back to the person's initials when an image file is missing.
  document.addEventListener(
    "error",
    function (e) {
      var img = e.target;
      if (!img || img.tagName !== "IMG" || img.hasAttribute("data-preview")) return;
      var box = img.closest && img.closest(".staff-photo[data-initials]");
      if (box) box.textContent = box.getAttribute("data-initials") || "";
    },
    true
  );

  IP.register("photo-preview", function (el) {
    var input = el.querySelector('input[type="file"]');
    var img = el.querySelector("[data-preview]");
    var placeholder = el.querySelector("[data-placeholder]");
    if (!input || !img) return;
    var lastUrl = null;
    input.addEventListener("change", function () {
      var file = input.files && input.files[0];
      if (lastUrl) {
        URL.revokeObjectURL(lastUrl);
        lastUrl = null;
      }
      if (!file || !/^image\//.test(file.type)) return;
      lastUrl = URL.createObjectURL(file);
      img.src = lastUrl;
      img.classList.remove("hidden");
      img.classList.add("size-full", "object-cover");
      if (placeholder) placeholder.classList.add("hidden");
    });
  });

  IP.register("currency-default", function (el) {
    var country = el.querySelector("[data-country]");
    var currency = el.querySelector("[data-currency]");
    if (!country || !currency) return;
    var map = {};
    try {
      map = JSON.parse(el.getAttribute("data-map") || "{}");
    } catch (e) {
      map = {};
    }
    var touched = false;
    currency.addEventListener("input", function () {
      touched = true;
      currency.value = currency.value.toUpperCase().replace(/[^A-Z]/g, "").slice(0, 3);
    });
    country.addEventListener("change", function () {
      var cur = map[country.value];
      if (cur && !touched) {
        currency.value = cur;
        currency.classList.add("ring-2", "ring-accent/40");
        setTimeout(function () {
          currency.classList.remove("ring-2", "ring-accent/40");
        }, 900);
      }
    });
  });

  IP.register("staff-form", function (form) {
    var role = form.querySelector('select[name="role"]');
    var hint = form.querySelector("[data-trainer-hint]");
    if (!role || !hint) return;
    var badge = IP.h("span", { class: "badge badge-violet ml-2 align-middle", text: "Needed for trainer approval" });
    var label = hint.querySelector("label");
    function sync() {
      var trainer = role.value === "trainer";
      if (label) {
        if (trainer && !badge.parentNode) label.appendChild(badge);
        if (!trainer && badge.parentNode) badge.parentNode.removeChild(badge);
      }
    }
    role.addEventListener("change", sync);
    sync();
  });

  IP.register("shift-form", function (form) {
    var boxes = Array.prototype.slice.call(form.querySelectorAll('input[name="weekdays"]'));
    var presets = form.querySelector("[data-weekday-presets]");
    if (presets) {
      presets.classList.remove("hidden");
      presets.classList.add("flex");
      presets.addEventListener("click", function (e) {
        var btn = e.target.closest("[data-preset]");
        if (!btn) return;
        e.preventDefault();
        var wanted = (btn.getAttribute("data-preset") || "").split(",").filter(Boolean);
        boxes.forEach(function (b) {
          b.checked = wanted.indexOf(b.value) !== -1;
        });
      });
    }
    var radios = Array.prototype.slice.call(form.querySelectorAll("[data-mode-radio]"));
    var branchField = form.querySelector("[data-branch-field]");
    function syncMode() {
      if (!branchField) return;
      var selected = radios.filter(function (r) {
        return r.checked;
      })[0];
      var online = selected && selected.value === "online";
      branchField.classList.toggle("hidden", !!online);
      var select = branchField.querySelector("select");
      if (select) select.disabled = !!online;
    }
    radios.forEach(function (r) {
      r.addEventListener("change", syncMode);
    });
    syncMode();
    form.addEventListener("submit", function (e) {
      if (!boxes.some(function (b) { return b.checked; })) {
        e.preventDefault();
        e.stopImmediatePropagation();
        if (IP.island) IP.island.notify({ title: "Pick a weekday", body: "Select at least one day for the shift.", tone: "warning" });
        if (boxes[0]) boxes[0].focus();
      }
    }, true);
  });

  IP.register("job-form", function (form) {
    var remote = form.querySelector("[data-remote-toggle]");
    var field = form.querySelector("[data-location-field]");
    if (!remote || !field) return;
    var input = field.querySelector("input");
    function sync() {
      var on = remote.checked;
      field.classList.toggle("opacity-50", on);
      if (input) {
        input.disabled = on;
        input.placeholder = on ? "Remote" : "City or area";
      }
    }
    remote.addEventListener("change", sync);
    sync();
  });
})();
