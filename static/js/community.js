(function () {
  var state = {
    monitors: [],
    query: "",
    country: "",
    feed: "",
    lastUpdated: null,
    map: null,
    markers: null,
  };

  function t(key) {
    if (window.t) return window.t(key);
    var pack = window.Translations && window.Translations.en;
    return (pack && pack[key]) || key;
  }

  function escapeHtml(value) {
    return String(value == null ? "" : value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function countryName(code) {
    if (!code) return t("community_unknown_country");
    try {
      var locale = document.documentElement.lang === "nl" ? "nl" : "en";
      return new Intl.DisplayNames([locale], { type: "region" }).of(code);
    } catch (error) {
      return code;
    }
  }

  function filtered() {
    var query = state.query.trim().toLowerCase();
    return state.monitors.filter(function (monitor) {
      if (state.country && monitor.country_code !== state.country) return false;
      if (state.feed && monitor.feed !== state.feed) return false;
      if (!query) return true;
      var name = (monitor.name || "").toLowerCase();
      var feed = (monitor.feed || "").toLowerCase();
      return name.indexOf(query) !== -1 || feed.indexOf(query) !== -1;
    });
  }

  function unique(key) {
    var seen = {};
    state.monitors.forEach(function (monitor) {
      if (monitor[key]) seen[monitor[key]] = true;
    });
    return Object.keys(seen).sort();
  }

  function fillSelect(select, values, allLabel, labelFor) {
    var current = select.value;
    select.replaceChildren();
    var all = document.createElement("option");
    all.value = "";
    all.textContent = allLabel;
    select.appendChild(all);
    values.forEach(function (value) {
      var option = document.createElement("option");
      option.value = value;
      option.textContent = labelFor ? labelFor(value) : value;
      select.appendChild(option);
    });
    if (values.indexOf(current) !== -1) select.value = current;
  }

  function renderStats(list) {
    document.getElementById("stat-monitors").textContent = String(state.monitors.length);
    document.getElementById("stat-feeds").textContent = String(unique("feed").length);
    document.getElementById("stat-countries").textContent = String(unique("country_code").length);
    var count = document.getElementById("community-count");
    var active = state.query || state.country || state.feed;
    count.hidden = !active;
    count.textContent = list.length + " " + t("community_of") + " " + state.monitors.length;
    var updated = document.getElementById("community-updated");
    if (state.lastUpdated) {
      var when = new Date(state.lastUpdated);
      updated.hidden = Number.isNaN(when.getTime());
      updated.textContent = t("community_updated") + " " + when.toLocaleString();
    } else {
      updated.hidden = true;
    }
  }

  function renderList(list) {
    var root = document.getElementById("community-list");
    root.replaceChildren();
    if (!state.monitors.length) {
      var empty = document.createElement("p");
      empty.className = "community-empty";
      empty.textContent = t("community_empty");
      root.appendChild(empty);
      return;
    }
    if (!list.length) {
      var none = document.createElement("p");
      none.className = "community-empty";
      none.textContent = t("community_no_match");
      root.appendChild(none);
      return;
    }
    var groups = {};
    list.forEach(function (monitor) {
      var code = monitor.country_code || "_unknown";
      if (!groups[code]) groups[code] = [];
      groups[code].push(monitor);
    });
    Object.keys(groups).sort(function (a, b) {
      if (a === "_unknown") return 1;
      if (b === "_unknown") return -1;
      return countryName(a).localeCompare(countryName(b));
    }).forEach(function (code) {
      var section = document.createElement("section");
      section.className = "community-country";
      var heading = document.createElement("h2");
      if (code !== "_unknown") {
        var flag = document.createElement("img");
        flag.alt = "";
        flag.src = "/static/flags/4x3/" + code.toLowerCase() + ".svg";
        heading.appendChild(flag);
      }
      var title = document.createElement("span");
      title.textContent = countryName(code === "_unknown" ? "" : code);
      heading.appendChild(title);
      var badge = document.createElement("span");
      badge.className = "count";
      badge.textContent = String(groups[code].length);
      heading.appendChild(badge);
      section.appendChild(heading);
      var grid = document.createElement("div");
      grid.className = "community-grid";
      groups[code].forEach(function (monitor) {
        grid.appendChild(card(monitor));
      });
      section.appendChild(grid);
      root.appendChild(section);
    });
  }

  function card(monitor) {
    var article = document.createElement("article");
    article.className = "monitor-card";
    var title = document.createElement("h3");
    title.textContent = monitor.name;
    article.appendChild(title);
    if (monitor.feed) {
      var feed = document.createElement("p");
      feed.textContent = t("community_feed") + ": " + monitor.feed;
      article.appendChild(feed);
    }
    if (monitor.url) {
      var link = document.createElement("p");
      var anchor = document.createElement("a");
      anchor.href = monitor.url;
      anchor.target = "_blank";
      anchor.rel = "noopener noreferrer";
      anchor.textContent = t("community_visit");
      link.appendChild(anchor);
      article.appendChild(link);
    } else if (monitor.local_only) {
      var local = document.createElement("p");
      local.textContent = t("community_local_only");
      article.appendChild(local);
    }
    return article;
  }

  // Every zoom moves each marker to a new spot within this radius of the
  // published (already blurred) position, as on the main map.
  var JITTER_METRES = 200;
  function jitter(lat, lon) {
    var maxLat = JITTER_METRES / 111320;
    var cosLat = Math.cos(lat * Math.PI / 180);
    var maxLon = cosLat > 0 ? JITTER_METRES / (111320 * cosLat) : maxLat;
    var angle = Math.random() * 2 * Math.PI;
    var dist = Math.sqrt(Math.random());
    return [lat + dist * maxLat * Math.cos(angle), lon + dist * maxLon * Math.sin(angle)];
  }

  function popup(monitor) {
    var html = "<strong>" + escapeHtml(monitor.name) + "</strong>";
    if (monitor.feed) html += "<br>" + escapeHtml(t("community_feed")) + ": " + escapeHtml(monitor.feed);
    if (monitor.url) {
      html += "<br><a href=\"" + escapeHtml(monitor.url) + "\" target=\"_blank\" rel=\"noopener noreferrer\">" + escapeHtml(t("community_visit")) + "</a>";
    } else if (monitor.local_only) {
      html += "<br>" + escapeHtml(t("community_local_only"));
    }
    return html;
  }

  function drawMarkers() {
    if (!state.map || !state.markers) return;
    state.markers.clearLayers();
    state.monitors.forEach(function (monitor) {
      if (typeof monitor.latitude !== "number" || typeof monitor.longitude !== "number") return;
      var point = jitter(monitor.latitude, monitor.longitude);
      var marker = L.marker(point);
      marker.bindPopup(popup(monitor));
      state.markers.addLayer(marker);
    });
  }

  function initMap() {
    if (typeof L === "undefined") return;
    // Leaflet prefixes its detected image folder to these URLs unless the
    // prefixing getter is removed first; with it, the path is doubled.
    delete L.Icon.Default.prototype._getIconUrl;
    L.Icon.Default.mergeOptions({
      iconUrl: "/static/vendor/leaflet/images/marker-icon.png",
      iconRetinaUrl: "/static/vendor/leaflet/images/marker-icon-2x.png",
      shadowUrl: "/static/vendor/leaflet/images/marker-shadow.png",
    });
    state.map = L.map("community-map").setView([20, 0], 2);
    L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
      attribution: "&copy; OpenStreetMap",
      maxZoom: 18,
    }).addTo(state.map);
    state.markers = L.markerClusterGroup ? L.markerClusterGroup({ maxClusterRadius: 50 }) : L.layerGroup();
    state.map.addLayer(state.markers);
    drawMarkers();
    state.map.on("zoomend", drawMarkers);
    if (state.markers.getLayers && state.markers.getLayers().length) {
      var bounds = state.markers.getBounds();
      if (bounds && bounds.isValid && bounds.isValid()) {
        state.map.fitBounds(bounds, { padding: [40, 40] });
      }
    }
  }

  function render() {
    var list = filtered();
    fillSelect(document.getElementById("community-country"), unique("country_code"), t("community_all_countries"), countryName);
    fillSelect(document.getElementById("community-feed"), unique("feed"), t("community_all_feeds"));
    document.getElementById("community-search").placeholder = t("community_search");
    renderStats(list);
    renderList(list);
    var clear = document.getElementById("community-clear");
    clear.hidden = !(state.query || state.country || state.feed);
  }

  function load() {
    fetch("/api/community/monitors")
      .then(function (response) { return response.json(); })
      .then(function (data) {
        state.monitors = Array.isArray(data.monitors) ? data.monitors : [];
        state.lastUpdated = data.last_updated || null;
        render();
        if (!state.map) initMap();
        else drawMarkers();
      })
      .catch(function () {
        state.monitors = [];
        render();
      });
  }

  document.addEventListener("DOMContentLoaded", function () {
    document.getElementById("community-search").addEventListener("input", function (event) {
      state.query = event.target.value;
      render();
    });
    document.getElementById("community-country").addEventListener("change", function (event) {
      state.country = event.target.value;
      render();
    });
    document.getElementById("community-feed").addEventListener("change", function (event) {
      state.feed = event.target.value;
      render();
    });
    document.getElementById("community-clear").addEventListener("click", function () {
      state.query = "";
      state.country = "";
      state.feed = "";
      document.getElementById("community-search").value = "";
      render();
    });
    window.addEventListener("languageChanged", function () {
      render();
      drawMarkers();
    });
    load();
    setTimeout(render, 300);
  });
})();
