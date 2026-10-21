// Loud passes per day: a stat row for today, a stacked bar chart over the last
// days, a table view of the same numbers, and the "loudest today" tile.
// Needs Chart.js on the page. Colours are one amber ramp, light to dark, with
// a separate set of steps for each theme so every bar clears 3:1 on its surface.
(function () {
  'use strict';

  const DEFAULT_LABELS = {
    title: 'Loud passes',
    subtitle: 'Passes by measured peak, per day. Night is 23:00 to 07:00.',
    today: 'Today',
    band_50: '50–55 dB',
    band_55: '55–60 dB',
    band_60: '60 dB and up',
    night: 'at night',
    with_db: 'measured',
    loudest: 'Loudest',
    loudest_today: 'Loudest today',
    none_yet: 'none yet',
    show_table: 'Table',
    show_chart: 'Chart',
    date: 'Date',
    passes: 'Passes',
    at: 'at',
    by_route: 'By route',
    route: 'Route',
    period: 'last {days} days',
  };

  const RAMP = {
    dark: ['#f6d58a', '#e2a122', '#a86f0e'],
    light: ['#b97c0f', '#8a5c0a', '#5a3c06'],
  };

  function isLight() {
    const attr = document.documentElement.getAttribute('data-theme') || document.body.getAttribute('data-theme') || '';
    return attr === 'light';
  }

  function token(name, fallback) {
    try {
      const v = window.themeManager && window.themeManager.token ? window.themeManager.token(name) : '';
      return v || getComputedStyle(document.documentElement).getPropertyValue('--' + name).trim() || fallback;
    } catch (e) { return fallback; }
  }

  function esc(v) {
    return String(v == null ? '' : v).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }

  function fmtDay(dateStr, locale) {
    const d = new Date(dateStr + 'T12:00:00');
    return d.toLocaleDateString(locale || undefined, { weekday: 'short', day: 'numeric', month: 'short' });
  }

  function mount(opts) {
    const o = opts || {};
    const L = Object.assign({}, DEFAULT_LABELS, o.labels || {});
    const card = document.getElementById(o.cardId);
    if (!card) return null;
    let chart = null;
    let data = null;
    let showTable = false;

    card.innerHTML = `
      <div class="noise-summary-head">
        <div>
          <h3>${esc(L.title)}</h3>
          <p class="small noise-summary-sub">${esc(L.subtitle)}</p>
        </div>
        <button type="button" class="noise-summary-toggle" aria-pressed="false">${esc(L.show_table)}</button>
      </div>
      <div class="noise-summary-stats" aria-live="polite"></div>
      <div class="noise-summary-chart"><canvas height="220" role="img" aria-label="${esc(L.title)}"></canvas></div>
      <div class="noise-summary-table" hidden></div>
      <p class="small noise-summary-loudest"></p>
      <div class="noise-summary-routes" hidden></div>`;

    const statsEl = card.querySelector('.noise-summary-stats');
    const chartWrap = card.querySelector('.noise-summary-chart');
    const canvas = card.querySelector('canvas');
    const tableEl = card.querySelector('.noise-summary-table');
    const loudestEl = card.querySelector('.noise-summary-loudest');
    const toggle = card.querySelector('.noise-summary-toggle');
    const routesEl = card.querySelector('.noise-summary-routes');
    let routeData = null;

    toggle.addEventListener('click', () => {
      showTable = !showTable;
      toggle.textContent = showTable ? L.show_chart : L.show_table;
      toggle.setAttribute('aria-pressed', String(showTable));
      chartWrap.hidden = showTable;
      tableEl.hidden = !showTable;
    });

    function stat(value, label) {
      return `<div class="noise-stat"><span class="noise-stat-value">${esc(value)}</span><span class="noise-stat-label">${esc(label)}</span></div>`;
    }

    function loudestText(l) {
      if (!l) return L.none_yet;
      const when = o.timeFn ? o.timeFn(l.ts) : '';
      const who = l.callsign ? `<a href="/callsign.html?c=${encodeURIComponent(l.callsign)}">${esc(l.callsign)}</a>` : '?';
      const max = l.spl_max_db != null ? ` (LAmax ${Number(l.spl_max_db).toFixed(1)})` : '';
      return `<strong>${Number(l.spl_db).toFixed(1)} dB</strong>${max} · ${who}${l.model ? ` · ${esc(l.model)}` : ''} ${esc(L.at)} ${esc(when)}`;
    }

    function render() {
      if (!data) return;
      const t = data.today || {};
      statsEl.innerHTML = [
        stat(t.band_50 + t.band_55 + t.band_60, `${L.today} ≥ 50 dB`),
        stat(t.band_55 + t.band_60, '≥ 55 dB'),
        stat(t.band_60, '≥ 60 dB'),
        stat(t.night_50, L.night),
        stat(`${t.with_db}/${t.passes}`, L.with_db),
      ].join('');
      loudestEl.innerHTML = `${esc(L.loudest)} (${data.days} d): ${loudestText(data.loudest)}`;

      const tile = o.loudestTileId ? document.getElementById(o.loudestTileId) : null;
      if (tile) {
        const l = t.loudest;
        tile.innerHTML = l ? `${Number(l.spl_db).toFixed(1)} dB <span class="small">${esc(l.callsign || '')}${o.timeFn ? ' · ' + esc(o.timeFn(l.ts)) : ''}</span>` : '–';
      }

      const ramp = RAMP[isLight() ? 'light' : 'dark'];
      const ink = token('theme-text-secondary', '#c4bfb4');
      const grid = token('theme-border-light', 'rgba(244,241,234,0.28)');
      const surface = token('theme-bg-card', isLight() ? '#fcfcfb' : '#171a15');
      const labels = data.series.map(d => fmtDay(d.date, o.locale));
      const sets = [
        { key: 'band_50', label: L.band_50, color: ramp[0] },
        { key: 'band_55', label: L.band_55, color: ramp[1] },
        { key: 'band_60', label: L.band_60, color: ramp[2] },
      ].map(s => ({
        label: s.label,
        data: data.series.map(d => d[s.key]),
        backgroundColor: s.color,
        borderColor: surface,     // a 2 px surface gap between stacked segments
        borderWidth: 2,
        borderRadius: 3,
        borderSkipped: false,
        maxBarThickness: 28,
      }));
      if (chart) chart.destroy();
      if (typeof Chart === 'undefined') return;
      chart = new Chart(canvas.getContext('2d'), {
        type: 'bar',
        data: { labels, datasets: sets },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          animation: false,
          interaction: { mode: 'index', intersect: false },
          scales: {
            x: { stacked: true, grid: { display: false }, ticks: { color: ink, maxRotation: 0, autoSkip: true }, border: { color: grid } },
            y: { stacked: true, beginAtZero: true, grid: { color: grid }, ticks: { color: ink, precision: 0 }, border: { display: false } },
          },
          plugins: {
            legend: { position: 'bottom', labels: { color: ink, boxWidth: 10, boxHeight: 10, usePointStyle: true, pointStyle: 'rectRounded' } },
            tooltip: {
              callbacks: {
                footer: (items) => {
                  const d = data.series[items[0].dataIndex];
                  const total = d.band_50 + d.band_55 + d.band_60;
                  const parts = [`≥ 50 dB: ${total}`, `${L.night}: ${d.night_50}`];
                  if (d.loudest) parts.push(`${L.loudest}: ${Number(d.loudest.spl_db).toFixed(1)} dB ${d.loudest.callsign || ''}`);
                  return parts.join('\n');
                },
              },
            },
          },
        },
      });

      tableEl.innerHTML = `<table class="noise-summary-grid"><thead><tr>
          <th>${esc(L.date)}</th><th>${esc(L.passes)}</th><th>${esc(L.band_50)}</th><th>${esc(L.band_55)}</th><th>${esc(L.band_60)}</th><th>${esc(L.night)}</th><th>${esc(L.loudest)}</th>
        </tr></thead><tbody>${data.series.slice().reverse().map(d => `<tr>
          <td>${esc(fmtDay(d.date, o.locale))}</td><td>${d.with_db}/${d.passes}</td><td>${d.band_50}</td><td>${d.band_55}</td><td>${d.band_60}</td><td>${d.night_50}</td>
          <td>${d.loudest ? `${Number(d.loudest.spl_db).toFixed(1)} dB ${esc(d.loudest.callsign || '')}` : '–'}</td>
        </tr>`).join('')}</tbody></table>`;
    }

    function renderRoutes() {
      if (!routeData || !routesEl) return;
      const rows = (routeData.period || []).filter(r => r.label && r.label !== 'unlabelled');
      if (!rows.length) { routesEl.hidden = true; return; }
      const todayBy = Object.fromEntries((routeData.today || []).map(r => [r.label, r]));
      routesEl.hidden = false;
      routesEl.innerHTML = `<h4 class="noise-summary-routes-title">${esc(L.by_route)} · ${esc(L.period.replace('{days}', routeData.days))}</h4>
        <table class="noise-summary-grid"><thead><tr>
          <th>${esc(L.route)}</th><th>${esc(L.today)}</th><th>${esc(L.passes)}</th><th>≥ 55 dB</th><th>${esc(L.loudest)}</th>
        </tr></thead><tbody>${rows.slice(0, 12).map(r => `<tr>
          <td>${esc(r.label)}</td><td>${(todayBy[r.label] || {}).passes || 0}</td><td>${r.passes}</td><td>${r.ge55 || 0}</td>
          <td>${r.max_db != null ? Number(r.max_db).toFixed(1) + ' dB' : '–'}</td>
        </tr>`).join('')}</tbody></table>`;
    }

    async function load() {
      try {
        const r = await fetch(`/api/stats/noise/daily?days=${o.days || 14}`, { cache: 'no-cache' });
        if (!r.ok) return;
        data = await r.json();
        render();
      } catch (e) { /* keep the last drawing */ }
      try {
        const r = await fetch(`/api/routes/daily?days=${o.days || 14}`, { cache: 'no-cache' });
        if (r.ok) { routeData = await r.json(); renderRoutes(); }
      } catch (e) { /* routes are optional */ }
    }

    load();
    const timer = setInterval(load, (o.refreshS || 300) * 1000);
    window.addEventListener('planenoise-theme', render);
    return { reload: load, stop: () => clearInterval(timer) };
  }

  window.NoiseSummary = { mount, DEFAULT_LABELS, RAMP };
})();
