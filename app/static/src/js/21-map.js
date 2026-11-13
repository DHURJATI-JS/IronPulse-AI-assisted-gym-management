/* WP-B maps (Leaflet is vendored at /static/vendor/leaflet/ and loaded only on pages that need it).
 *   data-component="branch-picker"  draggable pin + "Find address" (GET /api/geo/search) + "Use my location",
 *                                   keeps the lat/lon inputs ([data-lat] / [data-lon]) of the form in sync.
 *   data-component="map-view"       read-only map: data-points='{"pin":[lat,lon],"geocoded":[lat,lon],"label":"..."}'
 * Tiles come from the same-origin proxy /maps/tiles/{z}/{x}/{y}.png (Geoapify, OpenStreetMap fallback).
 */
(function () {
  "use strict";
  var IP = window.IP;
  if (!IP) return;

  var TILE_URL = "/maps/tiles/{z}/{x}/{y}.png";

  function attribution(geoapify) {
    return (
      '&copy; <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener">OpenStreetMap</a> contributors' +
      (geoapify ? ' &middot; <a href="https://www.geoapify.com/" target="_blank" rel="noopener">Geoapify</a>' : "")
    );
  }

  function pinIcon(alt) {
    return window.L.divIcon({
      className: "owner-pin-wrap",
      html: '<span class="owner-pin' + (alt ? " owner-pin-alt" : "") + '"></span>',
      iconSize: [30, 30],
      iconAnchor: [15, 15],
    });
  }

  function round6(n) {
    return Math.round(n * 1e6) / 1e6;
  }

  function validLatLon(lat, lon) {
    return isFinite(lat) && isFinite(lon) && lat >= -90 && lat <= 90 && lon >= -180 && lon <= 180 && !(lat === 0 && lon === 0);
  }

  /** Run init now if the element is visible, otherwise when its closed <details> opens (Leaflet needs a size). */
  function whenVisible(el, init) {
    var details = el.closest("details");
    if (details && !details.open) {
      var onToggle = function () {
        if (!details.open) return;
        details.removeEventListener("toggle", onToggle);
        setTimeout(init, 30);
      };
      details.addEventListener("toggle", onToggle);
      return;
    }
    init();
  }

  function leafletMissing(container) {
    var fb = container.querySelector(".owner-map-fallback");
    if (fb) {
      fb.textContent = "";
      fb.appendChild(IP.h("p", { class: "text-sm font-medium", text: "Map unavailable" }));
      fb.appendChild(IP.h("p", { class: "text-xs", text: "Type the latitude and longitude below instead." }));
    }
  }

  function baseMap(container, opts) {
    var fb = container.querySelector(".owner-map-fallback");
    if (fb) fb.remove();
    var map = window.L.map(container, {
      zoomControl: true,
      scrollWheelZoom: false,
      attributionControl: true,
      worldCopyJump: true,
    });
    window.L.tileLayer(TILE_URL, { maxZoom: 19, minZoom: 2, attribution: attribution(opts.geoapify) }).addTo(map);
    map.attributionControl.setPrefix(false);
    return map;
  }

  /* ------------------------------------------------------------------ editable pin */
  IP.register("branch-picker", function (root) {
    var container = root.querySelector("[data-map]");
    var form = root.closest("form") || document;
    var latInput = root.querySelector("[data-lat]");
    var lonInput = root.querySelector("[data-lon]");
    var status = root.querySelector("[data-picker-status]");
    var results = root.querySelector("[data-results]");
    var findBtn = root.querySelector("[data-find-address]");
    var locateBtn = root.querySelector("[data-locate]");
    var geoapify = root.getAttribute("data-geoapify") === "1";
    var center = [20, 10, 2];
    try {
      center = JSON.parse(root.getAttribute("data-center") || "[20,10,2]");
    } catch (e) {
      /* keep default */
    }
    var map = null;
    var marker = null;

    function say(text) {
      if (status) status.textContent = text;
    }

    function field(name) {
      return form.querySelector('[name="' + name + '"]');
    }

    function fillIfEmpty(name, value) {
      var f = field(name);
      if (f && value && !(f.value || "").trim()) {
        f.value = value;
        f.dispatchEvent(new Event("change", { bubbles: true }));
      }
    }

    function setInputs(lat, lon) {
      if (latInput) latInput.value = round6(lat).toFixed(6);
      if (lonInput) lonInput.value = round6(lon).toFixed(6);
      [latInput, lonInput].forEach(function (inp) {
        if (inp) inp.dispatchEvent(new Event("change", { bubbles: true }));
      });
    }

    function placeMarker(lat, lon, zoom) {
      if (!map) return;
      var ll = window.L.latLng(lat, lon);
      if (!marker) {
        marker = window.L.marker(ll, { draggable: true, icon: pinIcon(false), keyboard: true, title: "Branch entrance", alt: "Branch pin" }).addTo(map);
        marker.on("dragend", function () {
          var p = marker.getLatLng();
          setInputs(p.lat, p.lng);
          say("Pin moved - remember to save.");
        });
      } else {
        marker.setLatLng(ll);
      }
      map.setView(ll, zoom || Math.max(map.getZoom(), 16), { animate: true });
    }

    function init() {
      if (!window.L) return leafletMissing(container);
      map = baseMap(container, { geoapify: geoapify });
      var lat = parseFloat(latInput && latInput.value);
      var lon = parseFloat(lonInput && lonInput.value);
      if (validLatLon(lat, lon)) {
        map.setView([lat, lon], 16);
        placeMarker(lat, lon, 16);
      } else {
        map.setView([center[0], center[1]], center[2] || 5);
      }
      map.on("click", function (e) {
        placeMarker(e.latlng.lat, e.latlng.lng);
        setInputs(e.latlng.lat, e.latlng.lng);
        say("Pin placed - drag it to fine-tune, then save.");
      });
      setTimeout(function () {
        map.invalidateSize();
      }, 200);
    }

    // typing coordinates moves the pin
    [latInput, lonInput].forEach(function (inp) {
      if (!inp) return;
      inp.addEventListener("change", function () {
        var lat = parseFloat(latInput.value);
        var lon = parseFloat(lonInput.value);
        if (map && validLatLon(lat, lon)) placeMarker(lat, lon);
      });
    });

    function addressQuery() {
      var parts = [];
      ["address_line1", "address_line2", "city", "state", "postal_code"].forEach(function (name) {
        var f = field(name);
        if (f && (f.value || "").trim()) parts.push(f.value.trim());
      });
      var country = field("country_code");
      if (country && country.selectedIndex >= 0 && country.options[country.selectedIndex]) {
        parts.push(country.options[country.selectedIndex].text);
      }
      return parts.join(", ");
    }

    function choose(r) {
      if (results) {
        results.hidden = true;
        results.textContent = "";
      }
      if (!map) init();
      placeMarker(r.lat, r.lon, 17);
      setInputs(r.lat, r.lon);
      fillIfEmpty("city", r.city);
      fillIfEmpty("state", r.state);
      fillIfEmpty("postal_code", r.postcode);
      var tz = field("timezone");
      if (tz && r.timezone && tz.value !== r.timezone) {
        tz.value = r.timezone;
      }
      say("Pin set from “" + (r.label || "search result") + "”. Drag it to the exact entrance, then save.");
    }

    function showResults(list) {
      if (!results) return;
      results.textContent = "";
      list.forEach(function (r) {
        var sub = [r.city, r.state, r.postcode, (r.country_code || "").toUpperCase()].filter(Boolean).join(" · ");
        var btn = IP.h(
          "button",
          { type: "button", onclick: function () { choose(r); } },
          IP.h("span", { class: "mt-0.5 shrink-0 text-accent", text: "●" }),
          IP.h("span", { class: "min-w-0" },
            IP.h("span", { class: "block truncate font-medium", text: r.label || "Unnamed place" }),
            IP.h("span", { class: "block truncate text-xs text-zinc-500 dark:text-zinc-400", text: sub })
          )
        );
        results.appendChild(IP.h("li", {}, btn));
      });
      results.hidden = list.length === 0;
    }

    if (findBtn) {
      findBtn.addEventListener("click", function () {
        var q = addressQuery();
        if (q.length < 3) {
          say("Type the street and city first.");
          var l1 = field("address_line1");
          if (l1) l1.focus();
          return;
        }
        findBtn.disabled = true;
        say("Searching…");
        IP.api("/api/geo/search?q=" + encodeURIComponent(q.slice(0, 200)))
          .then(function (data) {
            var list = (data && data.results) || [];
            if (!list.length) {
              say("No match for that address - click the map to drop the pin yourself.");
              showResults([]);
              return;
            }
            if (list.length === 1) return choose(list[0]);
            showResults(list);
            say("Pick the matching address:");
          })
          .catch(function (err) {
            say(err.status === 401 ? "Please sign in again." : "Address search is unavailable - click the map instead.");
          })
          .then(function () {
            findBtn.disabled = false;
          });
      });
    }

    if (locateBtn) {
      locateBtn.addEventListener("click", function () {
        if (!navigator.geolocation) return say("Your browser can't share its location.");
        locateBtn.disabled = true;
        say("Locating…");
        navigator.geolocation.getCurrentPosition(
          function (pos) {
            locateBtn.disabled = false;
            var lat = pos.coords.latitude;
            var lon = pos.coords.longitude;
            if (!map) init();
            placeMarker(lat, lon, 17);
            setInputs(lat, lon);
            say("Pin set to your current position (±" + Math.round(pos.coords.accuracy || 0) + " m).");
            IP.api("/api/geo/reverse?lat=" + lat + "&lon=" + lon)
              .then(function (data) {
                var r = data && data.results && data.results[0];
                if (!r) return;
                fillIfEmpty("city", r.city);
                fillIfEmpty("state", r.state);
                fillIfEmpty("postal_code", r.postcode);
              })
              .catch(function () {});
          },
          function (err) {
            locateBtn.disabled = false;
            say(err && err.code === 1 ? "Location permission denied." : "Couldn't get your location.");
          },
          { enableHighAccuracy: true, timeout: 15000, maximumAge: 30000 }
        );
      });
    }

    whenVisible(container, init);
  });

  /* ------------------------------------------------------------------ read-only map (pin vs. geocoded address) */
  IP.register("map-view", function (container) {
    var data = {};
    try {
      data = JSON.parse(container.getAttribute("data-points") || "{}");
    } catch (e) {
      return;
    }
    whenVisible(container, function () {
      if (!window.L) return leafletMissing(container);
      var map = baseMap(container, { geoapify: container.getAttribute("data-geoapify") === "1" });
      var pts = [];
      if (data.pin) {
        window.L.marker(data.pin, { icon: pinIcon(false), title: data.label || "Pin", keyboard: false }).addTo(map);
        pts.push(data.pin);
      }
      if (data.geocoded) {
        window.L.marker(data.geocoded, { icon: pinIcon(true), title: "Typed address", keyboard: false }).addTo(map);
        pts.push(data.geocoded);
      }
      if (pts.length === 2) {
        window.L.polyline(pts, { color: "#5e5ce6", weight: 2, dashArray: "4 6", opacity: 0.8 }).addTo(map);
        map.fitBounds(pts, { padding: [40, 40], maxZoom: 17 });
      } else if (pts.length === 1) {
        map.setView(pts[0], 16);
      } else {
        map.setView([20, 10], 2);
      }
      setTimeout(function () {
        map.invalidateSize();
      }, 200);
    });
  });
})();
