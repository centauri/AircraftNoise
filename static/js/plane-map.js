// Map layers shared by the public pages: merged flight rows, noise range,
// recorded passes, flight paths and live aircraft. Needs Leaflet on the page.
(function () {
  'use strict';

  const FLIGHT_MERGE_WINDOW_S = 15 * 60;
  const TRACK_WINDOW_S = 15 * 60;
  const LIVE_REFRESH_MS = 30 * 1000;

  const DEFAULT_LABELS = {
    noise_range: 'Noise range',
    noise_range_hint: 'A plane gets a decibel when the straight line to the microphone is within {slant} km. At {alt} m that means within {ground} km over the ground.',
    live_aircraft: 'Live aircraft',
    updated: 'updated',
    in_noise_range: 'in noise range',
    outside_noise_range: 'outside noise range',
    no_decibel: 'no decibel',
    peak_at: 'peak at',
    altitude: 'altitude',
    speed: 'speed',
    slant: 'through the air',
    ground: 'over the ground',
    loud: 'loud',
    quiet: 'quiet',
    legend_passes: 'Recorded passes',
    legend_path: 'Flight path',
    unknown: 'Unknown',
  };

  function token(name, fallback) {
    try {
      if (window.themeManager && window.themeManager.token) {
        const v = window.themeManager.token(name);
        if (v) return v;
      }
      const v = getComputedStyle(document.documentElement).getPropertyValue('--' + name).trim();
      return v || fallback;
    } catch (e) {
      return fallback;
    }
  }

  function colors() {
    return {
      accent: token('theme-accent', '#e2a122'),
      quiet: token('theme-success', '#8fb56a'),
      loud: '#d9534f',
      none: token('theme-text-secondary', '#c4bfb4'),
      ink: token('theme-bg-primary', '#10120f'),
    };
  }

  function fmt(value, digits) {
    const n = Number(value);
    return Number.isFinite(n) ? n.toFixed(digits) : '–';
  }

  function esc(value) {
    return String(value == null ? '' : value)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }

  function slantKm(e) {
    const d = Number(e.distance_km);
    if (!Number.isFinite(d)) return Infinity;
    const alt = Number(e.altitude_m);
    const altKm = Number.isFinite(alt) ? alt / 1000 : 0;
    return Math.sqrt(d * d + altKm * altKm);
  }

  // ---- one row per flight -------------------------------------------------
  function mergeFlightRows(list) {
    const byAircraft = new Map();
    for (const e of list || []) {
      if (!e) continue;
      const key = e.icao24 || e.callsign || String(e.ts);
      if (!byAircraft.has(key)) byAircraft.set(key, []);
      byAircraft.get(key).push(e);
    }
    const merged = [];
    for (const rows of byAircraft.values()) {
      rows.sort((a, b) => (a.ts || 0) - (b.ts || 0));
      let group = [];
      for (const e of rows) {
        const last = group[group.length - 1];
        if (last && (e.ts || 0) - (last.ts || 0) > FLIGHT_MERGE_WINDOW_S) {
          merged.push(mergeFlightGroup(group));
          group = [];
        }
        group.push(e);
      }
      if (group.length) merged.push(mergeFlightGroup(group));
    }
    return merged;
  }

  function mergeFlightGroup(group) {
    const passes = group.filter(e => e.type === 'pass');
    const pool = passes.length ? passes : group;
    const base = Object.assign({}, pool.reduce((a, b) => ((b.ts || 0) > (a.ts || 0) ? b : a)));
    let nearest = null;
    for (const e of group) {
      const spl = Number(e.spl_db);
      if (e.spl_db != null && Number.isFinite(spl) && (base.spl_db == null || spl > Number(base.spl_db))) {
        base.spl_db = spl;
        base.spl_peak_ts = e.spl_peak_ts;
        base.spl_max_db = e.spl_max_db;
      }
      if (nearest === null || slantKm(e) < slantKm(nearest)) nearest = e;
      if (e.in_zone) base.in_zone = true;
    }
    if (nearest && Number.isFinite(Number(nearest.distance_km))) {
      base.distance_km = nearest.distance_km;
      base.altitude_m = nearest.altitude_m;
      if (nearest.latitude && nearest.longitude) {
        base.latitude = nearest.latitude;
        base.longitude = nearest.longitude;
      }
    }
    return base;
  }

  // ---- styling by decibel -------------------------------------------------
  function dbStyle(spl) {
    const c = colors();
    const v = Number(spl);
    if (spl == null || !Number.isFinite(v)) return { color: c.none, radius: 5, opacity: 0.6, fill: 0.15 };
    if (v < 45) return { color: c.quiet, radius: 6, opacity: 0.9, fill: 0.35 };
    if (v < 55) return { color: c.accent, radius: 8, opacity: 0.95, fill: 0.45 };
    return { color: c.loud, radius: 11, opacity: 1, fill: 0.55 };
  }

  // ---- layer group handling -----------------------------------------------
  function group(map, name) {
    map._planeLayers = map._planeLayers || {};
    if (!map._planeLayers[name]) map._planeLayers[name] = L.layerGroup().addTo(map);
    return map._planeLayers[name];
  }

  function groundReachKm(slantKm, altM) {
    const altKm = altM / 1000;
    const inside = slantKm * slantKm - altKm * altKm;
    return inside > 0 ? Math.sqrt(inside) : 0;
  }

  // ---- noise range --------------------------------------------------------
  function drawGate(map, info, labels) {
    const L_ = Object.assign({}, DEFAULT_LABELS, labels || {});
    const layer = group(map, 'gate');
    layer.clearLayers();
    if (!info || !info.sensor || !info.noise_slant_km) return;
    const c = colors();
    const slant = Number(info.noise_slant_km);
    const alt = Number(info.altitude_max_m) || 2000;
    const typical = Math.min(alt, 2000);
    const ground = groundReachKm(slant, typical);
    const hint = L_.noise_range_hint
      .replace('{slant}', slant.toFixed(1)).replace('{alt}', String(Math.round(typical))).replace('{ground}', ground.toFixed(1));
    L.circle(info.sensor, {
      radius: slant * 1000, color: c.accent, weight: 1.5, dashArray: '6 6', fill: false, interactive: true,
    }).bindTooltip(`<strong>${esc(L_.noise_range)}</strong><br>${esc(hint)}`, { sticky: true }).addTo(layer);
    if (ground > 0 && ground < slant) {
      L.circle(info.sensor, {
        radius: ground * 1000, color: c.accent, weight: 1, dashArray: '2 6', fill: false, interactive: false,
      }).addTo(layer);
    }
  }

  // ---- recorded passes ----------------------------------------------------
  function eventTooltip(e, labels, timeFn) {
    const L_ = labels;
    const when = timeFn ? timeFn(e.ts) : '';
    const spl = (e.spl_db != null)
      ? `<strong>${fmt(e.spl_db, 1)} dB</strong>${e.spl_max_db != null ? ` · LAmax ${fmt(e.spl_max_db, 1)} dB` : ''}`
      : esc(L_.no_decibel);
    const peak = (e.spl_db != null && e.spl_peak_ts && timeFn) ? ` · ${esc(L_.peak_at)} ${esc(timeFn(e.spl_peak_ts))}` : '';
    return `<strong>${esc(e.callsign || L_.unknown)}</strong> · ${esc(when)}<br>`
      + `${spl}${peak}<br>`
      + `${fmt(slantKm(e), 1)} km ${esc(L_.slant)} · ${fmt(e.distance_km, 1)} km ${esc(L_.ground)} · ${fmt(e.altitude_m, 0)} m`;
  }

  function drawEvents(map, events, opts) {
    const o = opts || {};
    const labels = Object.assign({}, DEFAULT_LABELS, o.labels || {});
    const layer = group(map, 'events');
    layer.clearLayers();
    const c = colors();
    for (const e of events || []) {
      if (!e || !e.latitude || !e.longitude) continue;
      const st = dbStyle(e.spl_db);
      const marker = L.circleMarker([e.latitude, e.longitude], {
        radius: st.radius, color: st.color, weight: 2, opacity: st.opacity,
        fillColor: st.color, fillOpacity: st.fill, aircraft: true,
      });
      marker.bindTooltip(eventTooltip(e, labels, o.timeFn), { direction: 'top', offset: [0, -st.radius] });
      if (o.onClick) marker.on('click', () => o.onClick(e));
      marker.addTo(layer);
    }
    return layer;
  }

  // ---- flight paths -------------------------------------------------------
  // The page redraws every 10 s; the same rows ask the server once per TRACK_CACHE_MS.
  const TRACK_CACHE_MS = 25 * 1000;
  let trackCache = { key: null, at: 0, tracks: {} };

  async function fetchTracks(codes, from, to) {
    const key = `${codes.join(',')}|${Math.floor(from)}|${Math.ceil(to)}`;
    if (trackCache.key === key && Date.now() - trackCache.at < TRACK_CACHE_MS) return trackCache.tracks;
    const r = await fetch(`/api/tracks?icao24=${encodeURIComponent(codes.join(','))}&from=${Math.floor(from)}&to=${Math.ceil(to)}`, { cache: 'no-cache' });
    if (!r.ok) throw new Error(`tracks ${r.status}`);
    const tracks = (await r.json()).tracks || {};
    trackCache = { key, at: Date.now(), tracks };
    return tracks;
  }

  async function drawTracks(map, events, opts) {
    const o = opts || {};
    const layer = group(map, 'tracks');
    layer.clearLayers();
    const rows = (events || []).filter(e => e && e.icao24 && e.ts);
    if (!rows.length) return layer;
    const codes = Array.from(new Set(rows.map(e => e.icao24))).slice(0, 100);
    const from = Math.min(...rows.map(e => e.ts)) - TRACK_WINDOW_S;
    const to = Math.max(...rows.map(e => e.ts)) + TRACK_WINDOW_S;
    let tracks = {};
    try {
      tracks = await fetchTracks(codes, from, to);
    } catch (err) {
      return layer;
    }
    const c = colors();
    for (const e of rows) {
      const samples = (tracks[e.icao24] || []).filter(s => Math.abs(s.ts - e.ts) <= TRACK_WINDOW_S);
      if (samples.length < 2) continue;
      const st = dbStyle(e.spl_db);
      const line = samples.map(s => [s.latitude, s.longitude]);
      L.polyline(line, { color: st.color, weight: 2, opacity: 0.7, aircraft: true }).addTo(layer);
      const last = samples[samples.length - 1];
      const prev = samples[samples.length - 2];
      L.marker([last.latitude, last.longitude], {
        icon: arrowIcon(headingBetween(prev, last), st.color, 10), interactive: false, aircraft: true,
      }).addTo(layer);
      if (e.spl_db != null && e.spl_peak_ts) {
        const peak = samples.reduce((a, b) => (Math.abs(b.ts - e.spl_peak_ts) < Math.abs(a.ts - e.spl_peak_ts) ? b : a));
        L.circleMarker([peak.latitude, peak.longitude], {
          radius: st.radius + 4, color: st.color, weight: 1.5, fill: false, dashArray: '2 3', interactive: false, aircraft: true,
        }).addTo(layer);
      }
    }
    return layer;
  }

  function headingBetween(a, b) {
    const toRad = d => d * Math.PI / 180;
    const y = Math.sin(toRad(b.longitude - a.longitude)) * Math.cos(toRad(b.latitude));
    const x = Math.cos(toRad(a.latitude)) * Math.sin(toRad(b.latitude))
      - Math.sin(toRad(a.latitude)) * Math.cos(toRad(b.latitude)) * Math.cos(toRad(b.longitude - a.longitude));
    return (Math.atan2(y, x) * 180 / Math.PI + 360) % 360;
  }

  function arrowIcon(heading, color, size) {
    const s = size || 18;
    const html = `<svg width="${s}" height="${s}" viewBox="0 0 20 20" style="transform:rotate(${Number(heading) || 0}deg);display:block">`
      + `<path d="M10 1 L16 17 L10 13 L4 17 Z" fill="${color}" stroke="${colors().ink}" stroke-width="1"/></svg>`;
    return L.divIcon({ html, className: 'plane-arrow', iconSize: [s, s], iconAnchor: [s / 2, s / 2] });
  }

  // ---- live aircraft ------------------------------------------------------
  function startLive(map, opts) {
    const o = opts || {};
    const labels = Object.assign({}, DEFAULT_LABELS, o.labels || {});
    const layer = group(map, 'live');
    const control = liveControl(map, labels);
    let timer = null;

    async function refresh() {
      if (document.visibilityState === 'hidden') return;
      try {
        const r = await fetch('/api/live_aircraft', { cache: 'no-cache' });
        if (!r.ok) return;
        const data = await r.json();
        layer.clearLayers();
        const c = colors();
        for (const a of data.aircraft || []) {
          if (!a || a.latitude == null || a.longitude == null) continue;
          const color = a.in_noise_range ? c.loud : (a.in_zone ? c.accent : c.none);
          const text = `<strong>${esc(a.callsign || a.icao24 || labels.unknown)}</strong><br>`
            + `${fmt(a.altitude_m, 0)} m · ${fmt(a.speed_kmh, 0)} km/h<br>`
            + `${fmt(a.slant_km != null ? a.slant_km : a.distance_km, 1)} km ${esc(labels.slant)} · `
            + esc(a.in_noise_range ? labels.in_noise_range : labels.outside_noise_range);
          L.marker([a.latitude, a.longitude], { icon: arrowIcon(a.heading_deg, color, 20), aircraft: true })
            .bindTooltip(text, { direction: 'top', offset: [0, -10] }).addTo(layer);
        }
        control.update(data.ts, (data.aircraft || []).length, o.timeFn);
        if (o.onGate && data.noise_slant_km) o.onGate(data);
      } catch (err) {
        /* keep the last drawing */
      }
    }

    refresh();
    timer = setInterval(refresh, LIVE_REFRESH_MS);
    document.addEventListener('visibilitychange', () => { if (document.visibilityState === 'visible') refresh(); });
    return { refresh, stop: () => clearInterval(timer) };
  }

  function liveControl(map, labels) {
    const ctl = L.control({ position: 'bottomleft' });
    let el = null;
    ctl.onAdd = function () {
      el = L.DomUtil.create('div', 'plane-map-legend');
      el.innerHTML = legendHtml(labels, null, 0, null);
      return el;
    };
    ctl.addTo(map);
    return {
      update(ts, count, timeFn) { if (el) el.innerHTML = legendHtml(labels, ts, count, timeFn); },
    };
  }

  function legendHtml(labels, ts, count, timeFn) {
    const c = colors();
    const dot = (color, r) => `<span style="display:inline-block;width:${r * 2}px;height:${r * 2}px;border-radius:50%;background:${color};opacity:.85;vertical-align:middle;margin-right:4px"></span>`;
    const live = ts ? `${esc(labels.live_aircraft)}: ${count} · ${esc(labels.updated)} ${esc(timeFn ? timeFn(ts) : '')}` : esc(labels.live_aircraft);
    return `<div>${live}</div>`
      + `<div>${esc(labels.legend_passes)}: ${dot(c.loud, 5)}≥55 dB ${dot(c.accent, 4)}45–55 ${dot(c.quiet, 3)}&lt;45 ${dot(c.none, 3)}${esc(labels.no_decibel)}</div>`;
  }

  window.PlaneMap = { mergeFlightRows, slantKm, dbStyle, drawGate, drawEvents, drawTracks, startLive, groundReachKm, DEFAULT_LABELS };
})();
