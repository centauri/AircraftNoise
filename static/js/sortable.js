// Click a column header to sort the table by it; click again to flip.
// Works on every table with a header, including ones a script fills later,
// because the click is handled at the document. A cell may carry data-sort
// with the value to sort by (a time as a number, say); otherwise its text is
// used, read as a number when it looks like one ("64.5 dB", "1,800 m", "32%").
// Tables with data-nosort and headers with class nosort are left alone.
(function () {
  'use strict';
  var style = document.createElement('style');
  style.textContent = 'thead th.sortable{cursor:pointer;user-select:none}thead th.sortable:hover{text-decoration:underline}' +
    'thead th[aria-sort=ascending]::after{content:" \\25B2";font-size:0.7em}thead th[aria-sort=descending]::after{content:" \\25BC";font-size:0.7em}';
  document.head.appendChild(style);

  function value(cell) {
    if (!cell) return null;
    if (cell.hasAttribute('data-sort')) {
      var raw = cell.getAttribute('data-sort');
      var n = parseFloat(raw);
      return raw !== '' && !isNaN(n) && /^-?[\d.]+(e[-+]?\d+)?$/i.test(raw.trim()) ? n : raw;
    }
    var text = (cell.textContent || '').trim();
    if (text === '' || text === '–' || text === '-' || text === '—') return null;
    var m = text.replace(/,/g, '').match(/^[≈~≥≤<>]?\s*(-?\d+(?:\.\d+)?)/);
    if (m && /^[≈~≥≤<>]?\s*-?\d/.test(text)) return parseFloat(m[1]);
    return text.toLowerCase();
  }

  function sortable(th) {
    if (!th || th.classList.contains('nosort')) return false;
    var table = th.closest('table');
    if (!table || table.hasAttribute('data-nosort') || !th.closest('thead')) return false;
    if (!(th.textContent || '').trim()) return false;
    return table;
  }

  function mark() {
    document.querySelectorAll('table thead th').forEach(function (th) {
      if (sortable(th)) th.classList.add('sortable');
    });
  }
  mark();
  new MutationObserver(mark).observe(document.documentElement, { childList: true, subtree: true });

  document.addEventListener('click', function (ev) {
    var th = ev.target.closest('th');
    var table = sortable(th);
    if (!table) return;
    if (ev.target.closest('a, button, input, select, form')) return;
    var index = Array.prototype.indexOf.call(th.parentNode.children, th);
    var body = table.tBodies[0];
    if (!body) return;
    var rows = Array.prototype.slice.call(body.rows);
    if (rows.length < 2) return;
    var values = rows.map(function (r) { return value(r.cells[index]); });
    if (values.every(function (v) { return v === null; })) return;
    var numeric = values.some(function (v) { return typeof v === 'number'; });
    var current = th.getAttribute('aria-sort');
    var dir = current ? (current === 'ascending' ? 'descending' : 'ascending') : (numeric ? 'descending' : 'ascending');
    Array.prototype.forEach.call(th.parentNode.children, function (h) { h.removeAttribute('aria-sort'); });
    th.setAttribute('aria-sort', dir);
    var sign = dir === 'ascending' ? 1 : -1;
    var order = rows.map(function (r, i) { return i; }).sort(function (a, b) {
      var va = values[a], vb = values[b];
      if (va === null && vb === null) return a - b;
      if (va === null) return 1;   // empty cells always last
      if (vb === null) return -1;
      if (typeof va === 'number' && typeof vb === 'number') return sign * (va - vb) || a - b;
      return sign * String(va).localeCompare(String(vb), undefined, { numeric: true }) || a - b;
    });
    order.forEach(function (i) { body.appendChild(rows[i]); });
  });
})();
