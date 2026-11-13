/* WP-A2 auth + account behaviour (progressive enhancement - every form also works without this file and the
 * server re-validates everything):
 *   password-field  show/hide toggle, live strength meter + checklist, "passwords match" hint
 *   signup-form     member/owner switch (photo requirement), minor -> guardian block, owner/staff age hints,
 *                   country follows the phone code, per-section completion + progress rail / mobile bar
 *   tz-hidden       fills a hidden <input name="timezone"> with the device time zone
 *   tz-select       "Use this device's time zone" + autofill when the account still says UTC
 *   photo-preview   instant preview of a chosen profile photo
 *   notif-list      mark one / all notifications read through the JSON API (keeps the island badge in sync)
 *   delete-confirm  enables the delete button only after typing DELETE
 */
(function () {
  "use strict";
  var IP = window.IP;

  var COMMON = [
    "password", "password1", "password123", "123456", "12345678", "123456789", "1234567890", "qwerty", "qwerty123",
    "111111", "abc123", "letmein", "welcome", "welcome1", "iloveyou", "admin", "admin123", "monkey", "dragon",
    "football", "baseball", "sunshine", "princess", "passw0rd", "p@ssw0rd", "gym12345", "fitness1", "ironpulse",
    "changeme", "trustno1", "superman", "batman", "000000", "987654321",
  ];
  var LEVEL_LABELS = [
    "Use 10+ characters with a mix of letters, numbers and symbols.",
    "Weak - keep going",
    "Almost there",
    "Good password",
    "Strong password",
  ];

  function each(list, fn) {
    Array.prototype.forEach.call(list || [], fn);
  }

  function ageFrom(value) {
    var m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value || "");
    if (!m) return null;
    var y = +m[1], mo = +m[2], d = +m[3];
    var now = new Date();
    var age = now.getFullYear() - y;
    if (now.getMonth() + 1 < mo || (now.getMonth() + 1 === mo && now.getDate() < d)) age -= 1;
    return age;
  }

  function personalInfo(form) {
    var scope = form || document;
    var pick = function (sel) {
      var el = scope.querySelector(sel);
      return el ? String(el.value || "") : "";
    };
    return {
      email: pick('[name="email"]') || pick("[data-personal-email]"),
      name: pick('[name="full_name"]') || pick("[data-personal-name]"),
    };
  }

  function containsPersonal(lower, info) {
    var local = (info.email || "").split("@")[0].toLowerCase();
    if (local.length >= 4 && lower.indexOf(local) !== -1) return true;
    var parts = (info.name || "").toLowerCase().split(/\s+/);
    for (var i = 0; i < parts.length; i++) {
      if (parts[i].length >= 4 && lower.indexOf(parts[i]) !== -1) return true;
    }
    return false;
  }

  /** Mirrors app.security.passwords.password_problems (the server stays the source of truth). */
  function evaluate(pw, info) {
    var classes = [/[a-z]/, /[A-Z]/, /\d/, /[^A-Za-z0-9]/].filter(function (re) {
      return re.test(pw);
    }).length;
    var lower = pw.toLowerCase();
    var rules = {
      length: pw.length >= 10 && pw.length <= 128,
      mix: classes >= 3,
      common: pw.length > 0 && COMMON.indexOf(lower) === -1,
      personal: pw.length > 0 && !containsPersonal(lower, info),
    };
    var passed = 0;
    Object.keys(rules).forEach(function (k) {
      if (rules[k]) passed += 1;
    });
    var level = 0;
    if (pw.length) {
      level = 1;
      if (passed === 4) level = pw.length >= 14 && classes === 4 ? 4 : 3;
      else if (passed === 3) level = 2;
    }
    return { rules: rules, level: level };
  }

  /* ---------------------------------------------------------------- password-field */
  IP.register("password-field", function (wrap) {
    var input = wrap.querySelector("[data-password-input]");
    if (!input) return;
    var form = input.form;
    var toggle = wrap.querySelector("[data-password-toggle]");
    if (toggle) {
      toggle.hidden = false;
      toggle.setAttribute("aria-label", "Show password");
      toggle.addEventListener("click", function () {
        var show = input.type === "password";
        input.type = show ? "text" : "password";
        toggle.textContent = show ? "Hide" : "Show";
        toggle.setAttribute("aria-pressed", show ? "true" : "false");
        toggle.setAttribute("aria-label", show ? "Hide password" : "Show password");
        input.focus();
      });
      if (form) {
        form.addEventListener("submit", function () {
          input.type = "password"; // never leave a revealed password on screen after submitting
        });
      }
    }

    if (wrap.getAttribute("data-strength") === "1") {
      var meter = wrap.querySelector("[data-strength-meter]");
      var bar = wrap.querySelector("[data-strength-bar]");
      var label = wrap.querySelector("[data-strength-label]");
      var rules = wrap.querySelectorAll("[data-rule]");
      var help = wrap.querySelector(".help");
      if (meter) meter.hidden = false;
      if (help && meter) help.hidden = true;
      var update = function () {
        var res = evaluate(input.value, personalInfo(form));
        if (bar) {
          bar.setAttribute("data-level", String(res.level));
          bar.style.width = res.level * 25 + "%";
        }
        if (label) label.textContent = LEVEL_LABELS[res.level];
        each(rules, function (li) {
          li.setAttribute("data-ok", res.rules[li.getAttribute("data-rule")] ? "1" : "0");
        });
        input.setCustomValidity(input.value && res.level < 3 ? "Choose a stronger password - see the checklist below the field." : "");
      };
      input.addEventListener("input", update);
      if (form) {
        form.addEventListener("input", function (e) {
          var n = e.target && e.target.name;
          if (n === "email" || n === "full_name") update();
        });
      }
      update();
    }

    var matchName = wrap.getAttribute("data-match");
    if (matchName && form) {
      var other = form.querySelector('[name="' + matchName + '"]');
      var hint = wrap.querySelector("[data-match-hint]");
      var check = function () {
        if (!input.value) {
          if (hint) hint.hidden = true;
          input.setCustomValidity("");
          return;
        }
        var ok = !!other && input.value === other.value;
        if (hint) {
          hint.hidden = false;
          hint.setAttribute("data-ok", ok ? "1" : "0");
          hint.textContent = ok ? "Passwords match" : "Passwords don't match yet";
        }
        input.setCustomValidity(ok ? "" : "The two passwords don't match.");
      };
      input.addEventListener("input", check);
      if (other) other.addEventListener("input", check);
    }
  });

  /* ---------------------------------------------------------------- tz-hidden / tz-select */
  IP.register("tz-hidden", function (el) {
    if (!el.value && IP.deviceTimezone) el.value = IP.deviceTimezone();
  });

  IP.register("tz-select", function (el) {
    var select = el.querySelector("[data-tz-input]");
    var btn = el.querySelector("[data-tz-device]");
    var hint = el.querySelector("[data-tz-hint]");
    var tz = IP.deviceTimezone ? IP.deviceTimezone() : "";
    if (!select || !tz) return;
    var ensure = function () {
      var found = false;
      each(select.options, function (o) {
        if (o.value === tz) found = true;
      });
      if (!found) select.appendChild(IP.h("option", { value: tz, text: tz }));
    };
    var apply = function (message) {
      ensure();
      select.value = tz;
      if (btn) btn.hidden = true;
      if (hint) hint.textContent = message;
    };
    if (btn && select.value !== tz) {
      btn.hidden = false;
      btn.textContent = "Use " + tz.replace(/_/g, " ") + " (this device)";
      btn.addEventListener("click", function () {
        apply("Set to this device's time zone - save to apply.");
      });
    }
    if (el.getAttribute("data-autofill") === "1" && (!select.value || select.value === "UTC") && tz !== "UTC") {
      apply("We picked " + tz.replace(/_/g, " ") + " from this device - save to apply.");
    }
  });

  /* ---------------------------------------------------------------- photo-preview */
  IP.register("photo-preview", function (el) {
    var input = el.querySelector("[data-photo-input]");
    var img = el.querySelector("[data-photo-img]");
    var placeholder = el.querySelector("[data-photo-placeholder]");
    if (!input || !img) return;
    var url = null;
    input.addEventListener("change", function () {
      var file = input.files && input.files[0];
      if (url) {
        URL.revokeObjectURL(url);
        url = null;
      }
      input.setCustomValidity("");
      if (!file) return;
      if (!/^image\/(jpeg|png|webp)$/.test(file.type)) {
        input.setCustomValidity("Choose a JPG, PNG or WEBP image.");
        input.reportValidity();
        return;
      }
      url = URL.createObjectURL(file);
      img.src = url;
      img.classList.remove("hidden");
      if (placeholder) placeholder.hidden = true;
    });
  });

  /* ---------------------------------------------------------------- signup-form (also the staff-invite form) */
  IP.register("signup-form", function (form) {
    var isStaff = form.getAttribute("data-staff") === "1";
    var dob = form.querySelector('[name="date_of_birth"]');
    var radios = form.querySelectorAll("[data-role-radio]");
    var guardianBlock = form.querySelector("[data-guardian-block]");
    var guardianFieldset = form.querySelector("[data-guardian-fieldset]");
    var guardianStep = document.querySelector("[data-guardian-step]");
    var photo = form.querySelector("[data-photo-input]");
    var photoMark = form.querySelector("[data-photo-required-mark]");
    var photoHelps = form.querySelectorAll("[data-photo-help]");
    var rail = document.querySelector("[data-signup-steps]");
    var mobile = form.querySelector("[data-signup-mobile-progress]");
    var sections = Array.prototype.slice.call(form.querySelectorAll("section[data-step]"));

    var role = function () {
      var r = form.querySelector("[data-role-radio]:checked");
      return r ? r.value : isStaff ? "staff" : "customer";
    };

    function syncRules() {
      var r = role();
      var age = dob ? ageFrom(dob.value) : null;
      var minor = !isStaff && r === "customer" && age !== null && age >= 0 && age < 18;
      if (guardianBlock) {
        var wasHidden = guardianBlock.hidden;
        guardianBlock.hidden = !minor;
        if (guardianFieldset) guardianFieldset.disabled = !minor;
        if (guardianStep) guardianStep.hidden = !minor;
        if (minor && wasHidden) {
          guardianBlock.classList.remove("animate-fade-up");
          void guardianBlock.offsetWidth; // restart the entrance animation
          guardianBlock.classList.add("animate-fade-up");
        }
      }
      if (!isStaff && photo) {
        var owner = r === "owner";
        photo.required = owner;
        if (photoMark) photoMark.hidden = !owner;
        each(photoHelps, function (h) {
          h.hidden = h.getAttribute("data-photo-help") !== (owner ? "owner" : "customer");
        });
      }
      if (dob) {
        var msg = "";
        if (age !== null && age < 0) msg = "Your date of birth can't be in the future.";
        else if (isStaff && age !== null && age < 16) msg = "Staff members must be at least 16 years old.";
        else if (!isStaff && r === "owner" && age !== null && age < 18) msg = "Gym owners must be at least 18 years old.";
        else if (age !== null && age < 5) msg = "You must be at least 5 years old.";
        dob.setCustomValidity(msg);
      }
    }

    function visibleSections() {
      return sections.filter(function (s) {
        return !s.hidden;
      });
    }

    function complete(section) {
      var ok = true;
      each(section.querySelectorAll("input, select, textarea"), function (f) {
        if (!ok || f.disabled || f.type === "hidden") return;
        if (!f.checkValidity()) ok = false;
      });
      return ok;
    }

    function titleOf(section) {
      var h = section.querySelector("h2");
      return h ? h.textContent.trim() : "";
    }

    var current = null;
    function refreshProgress() {
      var vis = visibleSections();
      var done = 0;
      vis.forEach(function (s) {
        var ok = complete(s);
        if (ok) done += 1;
        s.setAttribute("data-complete", ok ? "1" : "0");
        if (rail) {
          var link = rail.querySelector('[data-step-link="' + s.getAttribute("data-step") + '"]');
          if (link) link.setAttribute("data-done", ok ? "1" : "0");
        }
      });
      if (mobile && vis.length) {
        mobile.hidden = false;
        var bar = mobile.querySelector("[data-progress-bar]");
        var count = mobile.querySelector("[data-progress-count]");
        var label = mobile.querySelector("[data-progress-label]");
        if (bar) bar.style.width = Math.round((done / vis.length) * 100) + "%";
        if (count) count.textContent = done + " of " + vis.length + " done";
        if (label && current) label.textContent = titleOf(current);
      }
    }

    var queued = false;
    function schedule() {
      if (queued) return;
      queued = true;
      window.requestAnimationFrame(function () {
        queued = false;
        syncRules();
        refreshProgress();
      });
    }

    each(radios, function (r) {
      r.addEventListener("change", schedule);
    });
    form.addEventListener("input", schedule);
    form.addEventListener("change", schedule);

    // the country follows the phone's calling code until the person picks a country themselves
    var phoneCountry = form.querySelector('select[name="phone_country"]');
    var country = form.querySelector("[data-country-select]");
    var idCountry = form.querySelector('[name="id_country"]');
    var countryTouched = false;
    var idTouched = false;
    if (country) {
      country.addEventListener("change", function (e) {
        if (e.isTrusted) countryTouched = true;
        if (idCountry && !idTouched) idCountry.value = country.value;
      });
    }
    if (idCountry) {
      idCountry.addEventListener("input", function () {
        idTouched = true;
      });
    }
    if (phoneCountry && country) {
      phoneCountry.addEventListener("change", function () {
        if (countryTouched) return;
        var has = false;
        each(country.options, function (o) {
          if (o.value === phoneCountry.value) has = true;
        });
        if (has) {
          country.value = phoneCountry.value;
          if (idCountry && !idTouched) idCountry.value = phoneCountry.value;
        }
      });
    }

    // highlight the section in view on the progress rail
    if ("IntersectionObserver" in window && sections.length) {
      var io = new IntersectionObserver(
        function (entries) {
          entries.forEach(function (en) {
            if (!en.isIntersecting) return;
            current = en.target;
            var key = current.getAttribute("data-step");
            if (rail) {
              each(rail.querySelectorAll("[data-step-link]"), function (a) {
                if (a.getAttribute("data-step-link") === key) a.setAttribute("data-state", "current");
                else a.removeAttribute("data-state");
              });
            }
            refreshProgress();
          });
        },
        { rootMargin: "-30% 0px -60% 0px" }
      );
      sections.forEach(function (s) {
        io.observe(s);
      });
    }

    syncRules();
    refreshProgress();
  });

  /* ---------------------------------------------------------------- notif-list */
  IP.register("notif-list", function (root) {
    var totalEl = root.querySelector("[data-unread-total]");
    var readAll = document.querySelector("[data-notif-read-all]");

    function setUnread(n) {
      n = Math.max(0, n | 0);
      if (totalEl) totalEl.textContent = String(n);
      each(document.querySelectorAll("[data-unread-count]"), function (b) {
        b.textContent = String(n);
        b.hidden = n === 0;
      });
      if (readAll) readAll.hidden = n === 0;
      if (IP.island && IP.island.setUnread) IP.island.setUnread(n);
    }

    function markRow(row) {
      if (!row) return;
      row.setAttribute("data-unread", "0");
      var badge = row.querySelector("[data-new-badge]");
      if (badge) badge.remove();
      var f = row.querySelector("[data-notif-read]");
      if (f) f.remove();
    }

    root.addEventListener("submit", function (e) {
      var form = e.target.closest && e.target.closest("[data-notif-read]");
      if (!form) return;
      e.preventDefault();
      IP.api("/api/notifications/" + encodeURIComponent(form.getAttribute("data-id")) + "/read", { method: "POST" })
        .then(function (d) {
          markRow(form.closest("[data-notif]"));
          setUnread(d.unread || 0);
        })
        .catch(function () {
          form.submit();
        });
    });

    if (readAll) {
      readAll.addEventListener("submit", function (e) {
        e.preventDefault();
        IP.api("/api/notifications/read-all", { method: "POST" })
          .then(function () {
            each(root.querySelectorAll('[data-notif][data-unread="1"]'), markRow);
            setUnread(0);
            if (IP.island) IP.island.notify({ title: "All caught up", body: "Every notification is marked as read.", tone: "success", timeout: 2200 });
          })
          .catch(function () {
            readAll.submit();
          });
      });
    }

    IP.on("notification", function () {
      if (!totalEl) return;
      setUnread((parseInt(totalEl.textContent, 10) || 0) + 1);
    });
  });

  /* ---------------------------------------------------------------- delete-confirm */
  IP.register("delete-confirm", function (form) {
    var input = form.querySelector("[data-confirm-input]");
    var btn = form.querySelector("[data-confirm-submit]");
    if (!input || !btn) return;
    var sync = function () {
      var ok = input.value.trim() === "DELETE";
      btn.disabled = !ok;
      btn.setAttribute("aria-disabled", ok ? "false" : "true");
    };
    input.addEventListener("input", sync);
    sync();
  });
})();
