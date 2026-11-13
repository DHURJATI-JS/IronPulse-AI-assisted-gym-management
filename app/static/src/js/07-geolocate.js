/* "Use my location" component.
 *   <button data-component="geolocate" data-save="1" data-reload="1">Use my location</button>
 * - asks the browser for GPS coordinates (permission prompt), plus the device time zone
 * - data-save="1": POST /api/me/location {lat, lon, accuracy, timezone} (WP-A2 endpoint; signed-in users)
 * - fills hidden inputs name="lat" / name="lon" / name="timezone" in the closest <form> (if any)
 * - data-reload="1": reloads the page afterwards (server re-renders distance checks)
 * - emits IP.emit("location", {lat, lon, accuracy, timezone})
 */
(function () {
  "use strict";
  var IP = window.IP;

  IP.deviceTimezone = function () {
    try {
      return Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
    } catch (e) {
      return "UTC";
    }
  };

  IP.register("geolocate", function (btn) {
    btn.addEventListener("click", function (e) {
      e.preventDefault();
      if (!navigator.geolocation) {
        IP.island && IP.island.notify({ title: "Location unavailable", body: "Your browser does not support geolocation.", tone: "warning" });
        return;
      }
      var label = btn.textContent;
      btn.disabled = true;
      btn.textContent = "Locating…";
      navigator.geolocation.getCurrentPosition(
        function (pos) {
          var data = {
            lat: Math.round(pos.coords.latitude * 1e6) / 1e6,
            lon: Math.round(pos.coords.longitude * 1e6) / 1e6,
            accuracy: Math.round(pos.coords.accuracy || 0),
            timezone: IP.deviceTimezone(),
          };
          var form = btn.closest("form");
          if (form) {
            ["lat", "lon", "timezone"].forEach(function (k) {
              var input = form.querySelector('input[name="' + k + '"]');
              if (input) input.value = data[k];
            });
          }
          IP.emit("location", data);
          var done = function () {
            btn.disabled = false;
            btn.textContent = label;
            IP.island && IP.island.notify({ title: "Location updated", body: "Distances are now based on your current position.", tone: "success", timeout: 2200 });
            if (btn.getAttribute("data-reload") === "1") setTimeout(function () { location.reload(); }, 600);
          };
          if (btn.getAttribute("data-save") === "1" && document.body.getAttribute("data-ws") === "on") {
            IP.api("/api/me/location", { method: "POST", json: data }).then(done).catch(function (err) {
              btn.disabled = false;
              btn.textContent = label;
              IP.island && IP.island.notify({ title: "Couldn't save location", body: err.message, tone: "danger" });
            });
          } else done();
        },
        function (err) {
          btn.disabled = false;
          btn.textContent = label;
          IP.island && IP.island.notify({ title: "Location blocked", body: err.message || "Permission denied.", tone: "warning" });
        },
        { enableHighAccuracy: true, timeout: 15000, maximumAge: 60000 }
      );
    });
  });
})();
