/* WP-A1 public site behaviour (strict CSP: no inline handlers; DOM built with IP.h / textContent).
 *
 *   data-component="ai-live"        live AI status chip (WebSocket events when signed in, polling otherwise)
 *   data-component="geo-form"       GET form + foundation "geolocate" button: submits lat/lon once located
 *   data-component="gallery"        photo grid -> full-screen lightbox (keyboard, swipe, focus handling)
 *   data-component="branch-map"     Leaflet map from data-lat / data-lon (tiles via /maps/tiles proxy)
 *   data-component="distance-sort"  children with data-lat/data-lon get distance chips + nearest-first order
 *   IP.geo.haversine / formatKm / sortByDistance / lastLocation   distance helpers
 */
(function () {
  "use strict";
  var IP = window.IP;
  if (!IP) return;

  /* ---------------------------------------------------------------- geo helpers */
  var EARTH_KM = 6371.0088;
  var LOC_KEY = "ip-last-location";
  var LOC_MAX_AGE = 30 * 60 * 1000;

  function toRad(d) {
    return (d * Math.PI) / 180;
  }
  function num(v) {
    var n = parseFloat(v);
    return isFinite(n) ? n : null;
  }
  function haversine(lat1, lon1, lat2, lon2) {
    var dLat = toRad(lat2 - lat1);
    var dLon = toRad(lon2 - lon1);
    var a = Math.pow(Math.sin(dLat / 2), 2) + Math.cos(toRad(lat1)) * Math.cos(toRad(lat2)) * Math.pow(Math.sin(dLon / 2), 2);
    return 2 * EARTH_KM * Math.asin(Math.min(1, Math.sqrt(a)));
  }
  function formatKm(d) {
    if (d == null || !isFinite(d)) return "";
    if (d < 1) return Math.max(10, Math.round(d * 100) * 10) + " m";
    if (d < 10) return d.toFixed(1) + " km";
    return Math.round(d) + " km";
  }
  /** [{el, lat, lon}] or elements with data-lat/data-lon -> [{el, km}] nearest first (unknown last). */
  function sortByDistance(items, lat, lon) {
    return Array.prototype.map
      .call(items, function (it) {
        var plain = it && it.el !== undefined; // {el, lat, lon} objects or DOM elements
        var el = plain ? it.el : it;
        var la = plain ? num(it.lat) : num(el.getAttribute("data-lat"));
        var lo = plain ? num(it.lon) : num(el.getAttribute("data-lon"));
        return { el: el, km: la == null || lo == null ? null : haversine(lat, lon, la, lo) };
      })
      .sort(function (a, b) {
        if (a.km == null) return b.km == null ? 0 : 1;
        if (b.km == null) return -1;
        return a.km - b.km;
      });
  }
  function rememberLocation(loc) {
    try {
      sessionStorage.setItem(LOC_KEY, JSON.stringify({ lat: loc.lat, lon: loc.lon, t: Date.now() }));
    } catch (e) {}
  }
  function lastLocation() {
    try {
      var v = JSON.parse(sessionStorage.getItem(LOC_KEY) || "null");
      if (v && num(v.lat) != null && num(v.lon) != null && Date.now() - (v.t || 0) < LOC_MAX_AGE) return v;
    } catch (e) {}
    return null;
  }
  IP.geo = { haversine: haversine, formatKm: formatKm, sortByDistance: sortByDistance, lastLocation: lastLocation };
  IP.on("location", rememberLocation);

  /* ---------------------------------------------------------------- tiny SVG icons for JS-built UI */
  var PATHS = { x: "M18 6 6 18M6 6l12 12", left: "M15 18l-6-6 6-6", right: "M9 18l6-6-6-6", pin: "M21 10c0 7-9 13-9 13s-9-6-9-13a9 9 0 0 1 18 0z" };
  function svgIcon(name, cls) {
    var ns = "http://www.w3.org/2000/svg";
    var svg = document.createElementNS(ns, "svg");
    svg.setAttribute("viewBox", "0 0 24 24");
    svg.setAttribute("fill", "none");
    svg.setAttribute("stroke", "currentColor");
    svg.setAttribute("stroke-width", "2");
    svg.setAttribute("stroke-linecap", "round");
    svg.setAttribute("stroke-linejoin", "round");
    svg.setAttribute("class", cls || "size-5");
    svg.setAttribute("aria-hidden", "true");
    var p = document.createElementNS(ns, "path");
    p.setAttribute("d", PATHS[name]);
    svg.appendChild(p);
    return svg;
  }

  /* ---------------------------------------------------------------- live AI status chip */
  IP.register("ai-live", function (el) {
    var text = el.querySelector("[data-ai-live-text]");
    var dot = el.querySelector("[data-ai-dot]");
    var prefix = el.getAttribute("data-online-prefix") || "AI online";
    function paint(available, state, label) {
      if (dot) dot.setAttribute("data-state", state || (available ? "online" : "offline"));
      if (text) text.textContent = available ? prefix + (label ? " · " + label : "") : "AI unavailable · videos & safety rules still work";
    }
    IP.on("hello", function (evt) {
      if (evt && evt.ai) paint(evt.ai.state !== "offline", evt.ai.state, evt.ai.active);
    });
    IP.on("ai.status", function (evt) {
      paint(!!evt.available, evt.state, evt.active);
    });
    if (document.body.getAttribute("data-ws") !== "on") {
      // guests have no WebSocket: refresh from the public status endpoint while the tab is visible
      setInterval(function () {
        if (document.hidden) return;
        IP.api("/api/ai/status")
          .then(function (s) {
            paint(!!s.available, s.state, s.active);
          })
          .catch(function () {});
      }, 60000);
    }
  });

  /* ---------------------------------------------------------------- "Use my location" forms */
  IP.register("geo-form", function (form) {
    var armed = false;
    form.addEventListener(
      "click",
      function (e) {
        if (e.target.closest && e.target.closest('[data-component~="geolocate"]')) armed = true;
      },
      true
    );
    IP.on("location", function (loc) {
      if (!armed) return;
      armed = false;
      ["lat", "lon"].forEach(function (k) {
        var input = form.querySelector('input[name="' + k + '"]');
        if (input) input.value = loc[k];
      });
      var sent = false;
      function go() {
        if (sent) return;
        sent = true;
        form.submit();
      }
      if (document.body.getAttribute("data-ws") === "on") {
        // signed in: remember it on the account too (never block the page on it)
        setTimeout(go, 2500);
        IP.api("/api/me/location", { method: "POST", json: loc })
          .catch(function () {})
          .then(go);
      } else {
        go();
      }
    });
  });

  /* ---------------------------------------------------------------- gallery lightbox */
  IP.register("gallery", function (root) {
    var links = Array.prototype.slice.call(root.querySelectorAll("[data-gallery-item]"));
    if (!links.length) return;
    var photos = links.map(function (a) {
      return { src: a.getAttribute("href"), caption: a.getAttribute("data-caption") || "" };
    });
    var n = photos.length;
    var box, img, caption, counter, closeBtn, prevBtn, nextBtn;
    var index = 0;
    var lastFocus = null;
    var prevOverflow = "";

    function build() {
      img = IP.h("img", { class: "pub-lightbox-img", alt: "", draggable: "false" });
      caption = IP.h("p", { class: "pub-lightbox-caption" });
      counter = IP.h("p", { class: "shrink-0 text-xs font-semibold tabular-nums text-white/60", "aria-live": "polite" });
      closeBtn = IP.h("button", { type: "button", class: "pub-lightbox-btn", "aria-label": "Close gallery" }, svgIcon("x"));
      prevBtn = IP.h("button", { type: "button", class: "pub-lightbox-btn pub-lightbox-nav left-4", "aria-label": "Previous photo" }, svgIcon("left"));
      nextBtn = IP.h("button", { type: "button", class: "pub-lightbox-btn pub-lightbox-nav right-4", "aria-label": "Next photo" }, svgIcon("right"));
      var stage = IP.h("div", { class: "pub-lightbox-stage" }, prevBtn, img, nextBtn);
      box = IP.h(
        "div",
        { class: "pub-lightbox", role: "dialog", "aria-modal": "true", "aria-label": "Photo gallery", tabindex: "-1", hidden: true },
        IP.h("div", { class: "pub-lightbox-bar" }, counter, closeBtn),
        stage,
        IP.h("div", { class: "pub-lightbox-bar justify-center pb-6" }, caption)
      );
      closeBtn.addEventListener("click", close);
      prevBtn.addEventListener("click", function () {
        show(index - 1);
      });
      nextBtn.addEventListener("click", function () {
        show(index + 1);
      });
      stage.addEventListener("click", function (e) {
        if (e.target === stage) close();
      });
      // swipe left/right to browse, swipe down to close (touch + pen)
      var x0 = null;
      var y0 = null;
      box.addEventListener(
        "touchstart",
        function (e) {
          var t = e.touches[0];
          x0 = t.clientX;
          y0 = t.clientY;
        },
        { passive: true }
      );
      box.addEventListener("touchend", function (e) {
        if (x0 == null) return;
        var t = e.changedTouches[0];
        var dx = t.clientX - x0;
        var dy = t.clientY - y0;
        x0 = y0 = null;
        if (Math.abs(dx) > 45 && Math.abs(dx) > Math.abs(dy) * 1.2) show(index + (dx < 0 ? 1 : -1));
        else if (dy > 90 && Math.abs(dy) > Math.abs(dx)) close();
      });
      // document-level so arrows/Escape keep working after a click moves focus onto the photo
      document.addEventListener("keydown", function (e) {
        if (box.hidden) return;
        if (e.key === "Escape") {
          e.preventDefault();
          close();
        } else if (e.key === "ArrowLeft") {
          e.preventDefault();
          show(index - 1);
        } else if (e.key === "ArrowRight") {
          e.preventDefault();
          show(index + 1);
        } else if (e.key === "Tab") {
          // keep focus inside the dialog
          var f = [closeBtn, prevBtn, nextBtn].filter(function (b) {
            return b.offsetParent !== null;
          });
          var i = f.indexOf(document.activeElement);
          e.preventDefault();
          f[(i + (e.shiftKey ? -1 : 1) + f.length) % f.length].focus();
        }
      });
      if (n < 2) {
        prevBtn.hidden = true;
        nextBtn.hidden = true;
      }
      document.body.appendChild(box);
    }

    function preload(i) {
      var p = photos[(i + n) % n];
      if (p) new Image().src = p.src;
    }

    function show(i) {
      index = (i + n) % n;
      var p = photos[index];
      if (img.getAttribute("src") !== p.src) {
        // fade in once loaded (also on error, so the alt text of a missing photo stays visible)
        img.style.opacity = "0";
        img.onload = img.onerror = function () {
          img.style.opacity = "1";
        };
        img.src = p.src;
      }
      img.alt = p.caption;
      caption.textContent = p.caption;
      counter.textContent = index + 1 + " / " + n;
      preload(index + 1);
      preload(index - 1);
    }

    function open(i) {
      if (!box) build();
      lastFocus = document.activeElement;
      prevOverflow = document.documentElement.style.overflow;
      document.documentElement.style.overflow = "hidden";
      box.hidden = false;
      show(i);
      closeBtn.focus();
    }

    function close() {
      if (!box || box.hidden) return;
      box.hidden = true;
      document.documentElement.style.overflow = prevOverflow;
      if (lastFocus && lastFocus.focus) lastFocus.focus();
    }

    links.forEach(function (a, i) {
      a.addEventListener("click", function (e) {
        if (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey || e.button !== 0) return;
        e.preventDefault();
        open(i);
      });
    });
  });

  /* ---------------------------------------------------------------- branch map (vendored Leaflet) */
  IP.register("branch-map", function (el) {
    var lat = num(el.getAttribute("data-lat"));
    var lon = num(el.getAttribute("data-lon"));
    var fallback = el.querySelector("[data-map-fallback]");
    function fail(msg) {
      var p = fallback && fallback.querySelector("p");
      if (p) p.textContent = msg;
    }
    if (lat == null || lon == null) return fail("This branch hasn't been pinned on the map yet.");
    var L = window.L;
    if (!L || !L.map) return fail("The map couldn't load - use the OpenStreetMap link below.");
    if (fallback) fallback.remove();
    var map = L.map(el, { center: [lat, lon], zoom: 15, scrollWheelZoom: false, zoomControl: true });
    map.attributionControl.setPrefix(false);
    L.tileLayer("/maps/tiles/{z}/{x}/{y}.png", {
      minZoom: 3,
      maxZoom: 19,
      attribution: '&copy; <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener noreferrer">OpenStreetMap</a> contributors',
    }).addTo(map);
    var pin = L.divIcon({ className: "pub-map-pin", html: "<span></span>", iconSize: [26, 26], iconAnchor: [13, 13] });
    L.marker([lat, lon], { icon: pin, keyboard: false, title: el.getAttribute("data-name") || "" }).addTo(map);
    // scroll-zoom only after the visitor engages with the map (page scrolling stays smooth)
    map.once("click focus", function () {
      map.scrollWheelZoom.enable();
    });
    setTimeout(function () {
      map.invalidateSize();
    }, 300);
    var last = lastLocation();
    if (last) {
      var you = L.circleMarker([last.lat, last.lon], { radius: 7, weight: 3, color: "#ffffff", fillColor: "#30d158", fillOpacity: 1 });
      you.addTo(map);
      if (haversine(lat, lon, last.lat, last.lon) < 40) map.fitBounds(L.latLngBounds([[lat, lon], [last.lat, last.lon]]).pad(0.35), { maxZoom: 15 });
    }
  });

  /* ---------------------------------------------------------------- nearest-first lists */
  IP.register("distance-sort", function (list) {
    function apply(lat, lon) {
      var sorted = sortByDistance(list.children, lat, lon);
      sorted.forEach(function (row) {
        list.appendChild(row.el);
        if (row.km == null) return;
        var chip = row.el.querySelector("[data-distance]");
        if (chip) {
          chip.textContent = formatKm(row.km);
        } else {
          var cover = row.el.querySelector(".pub-cover");
          if (cover) {
            cover.appendChild(
              IP.h("span", { class: "pub-photo-badge absolute bottom-3 left-3" }, svgIcon("pin", "size-3.5 text-accent"), IP.h("span", { "data-distance": "", text: formatKm(row.km) }))
            );
          }
        }
      });
    }
    // server already sorted when it knew the location; otherwise use this tab's last shared location
    if (!list.querySelector("[data-distance]")) {
      var last = lastLocation();
      if (last) apply(last.lat, last.lon);
    }
    IP.on("location", function (loc) {
      apply(loc.lat, loc.lon);
    });
  });
})();
