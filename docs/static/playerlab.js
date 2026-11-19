/* Player Lab (update 81): filters, chart, stats, court, shot types, similar players. Data: #lab-data JSON. */
(function () {
  var el = document.getElementById('lab-data'); if (!el) return;
  var D = JSON.parse(el.textContent), root = document.getElementById('lab');
  var LAB = {pts: 'PTS', reb: 'REB', ast: 'AST', fg3m: '3PM', pra: 'PRA', pa: 'P+A', pr: 'P+R', ra: 'R+A'};
  var S = {stat: D.first || 'pts', n: 15, venue: 'all', vs: false, b2b: false, margin: 'all', min: 0, mode: 'avg', court: 'player'};
  function $(q) { return root.querySelector(q); }
  function esc(s) { return String(s).replace(/[&<>"]/g, function (c) { return {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}[c]; }); }
  function med(a) { if (!a.length) return null; var b = a.slice().sort(function (x, y) { return x - y; }), m = b.length >> 1; return b.length % 2 ? b[m] : (b[m - 1] + b[m]) / 2; }
  function avg(a) { return a.length ? a.reduce(function (x, y) { return x + y; }, 0) / a.length : null; }
  function f1(x) { return x == null ? '-' : (Math.round(x * 10) / 10).toFixed(1); }
  function pctOdds(p) { return p == null ? '' : Math.round(p * 100) + '%'; }

  function rows() {
    var r = D.log.filter(function (g) {
      if (S.venue === 'home' && !g.home) return false;
      if (S.venue === 'away' && g.home) return false;
      if (S.vs && g.opp !== D.opp_raw) return false;
      if (S.b2b && !g.b2b) return false;
      if (S.margin === 'won' && g.m < 10) return false;
      if (S.margin === 'lost' && g.m > -10) return false;
      if (g.min < S.min) return false;
      return true;
    });
    return S.n ? r.slice(-S.n) : r;
  }
  function line() { var t = D.tonight[S.stat]; return t && t.line != null ? t.line : null; }

  function chart(r) {
    var ln = line(), W = 760, H = 270, pad = 32, bw = Math.max(8, Math.min(34, (W - pad * 2) / Math.max(1, r.length) - 6));
    var mx = Math.max.apply(null, [ln || 0].concat(r.map(function (g) { return g.v[S.stat]; }))) * 1.15 || 1;
    var y = function (v) { return H - 34 - (H - 60) * v / mx; };
    var s = '<svg viewBox="0 0 ' + W + ' ' + H + '" class="lab-svg" role="img" aria-label="Game by game ' + LAB[S.stat] + '">';
    r.forEach(function (g, i) {
      var v = g.v[S.stat], x = pad + i * ((W - pad * 2) / Math.max(1, r.length)) + 3;
      var over = ln != null && v > ln, col = ln == null ? '#8a8f98' : (over ? '#2fd47a' : '#e8344e');
      var h = Math.max(3, (H - 34) - y(v));
      s += '<rect x="' + x + '" y="' + (H - 34 - h) + '" width="' + bw + '" height="' + h + '" fill="' + col + '"><title>' + g.d + ' vs ' + g.opp + ': ' + v + '</title></rect>';
      s += '<text x="' + (x + bw / 2) + '" y="' + (H - 34 - h - 4) + '" class="lab-val">' + v + '</text>';
      s += '<text x="' + (x + bw / 2) + '" y="' + (H - 18) + '" class="lab-ax">' + esc(g.opp) + '</text>';
      s += '<text x="' + (x + bw / 2) + '" y="' + (H - 6) + '" class="lab-ax2">' + g.d.slice(5).replace('-', '/') + '</text>';
      if (g.ln[S.stat] != null) s += '<line x1="' + (x - 2) + '" x2="' + (x + bw + 2) + '" y1="' + y(g.ln[S.stat]) + '" y2="' + y(g.ln[S.stat]) + '" class="lab-tick"/>';
    });
    if (ln != null) s += '<line x1="' + pad + '" x2="' + (W - pad) + '" y1="' + y(ln) + '" y2="' + y(ln) + '" class="lab-line"/><text x="4" y="' + (y(ln) + 4) + '" class="lab-lnlab">' + ln + '</text>';
    return s + '</svg>';
  }

  function head(r) {
    var ln = line(), t = D.tonight[S.stat] || {}, n = r.length, o = 0;
    r.forEach(function (g) { if (ln != null && g.v[S.stat] > ln) o++; });
    var rate = n && ln != null ? Math.round(100 * o / n) : null;
    var imp = t.be_over != null ? Math.round(t.be_over * 100) : null;
    var cls = rate == null || imp == null ? '' : (rate > imp ? 'stat-good' : 'stat-bad');
    return '<div class="hv-rec pz-board lab-head">' +
      '<div class="hv-tile"><b>' + (ln != null ? ln : '-') + '</b><span>tonight\'s ' + LAB[S.stat] + ' line' + (t.call ? ' &middot; our call ' + t.call : '') + '</span></div>' +
      '<div class="hv-tile"><b class="' + cls + '">' + (rate == null ? '-' : rate + '%') + '</b><span>' + (ln == null ? 'no ' + LAB[S.stat] + ' line tonight' : 'over in ' + o + ' of ' + n + ' shown games') + '</span></div>' +
      '<div class="hv-tile"><b>' + (imp == null ? '-' : imp + '%') + '</b><span>price implies over</span></div>' +
      '<div class="hv-tile"><b>' + f1(S.mode === 'avg' ? avg(r.map(function (g) { return g.v[S.stat]; })) : med(r.map(function (g) { return g.v[S.stat]; }))) + '</b><span>' + (S.mode === 'avg' ? 'average' : 'median') + ' in shown games</span></div></div>';
  }

  function stats(r) {
    var f = S.mode === 'avg' ? avg : med, cells = [['MIN', 'min'], ['FGA', 'fga'], ['3PA', 'fg3a'], ['FTA', 'fta'], ['USG', 'usg']];
    return '<div class="lab-stats">' + cells.map(function (c) { return '<div><b>' + f1(f(r.map(function (g) { return g[c[1]]; }))) + '</b><span>' + c[0] + '</span></div>'; }).join('') + '</div>';
  }

  function rankTxt(rk) { if (!rk) return '<span class="dim">-</span>'; var n = D.nteams || 30, c = rk[1] <= 10 ? 'stat-good' : (rk[1] > n - 10 ? 'stat-bad' : ''); return '<span class="' + c + '">' + ordinal(rk[1]) + ' most</span> <span class="dim small">(' + rk[0] + '/g)</span>'; }
  function ordinal(n) { var s = ['th', 'st', 'nd', 'rd'], v = n % 100; return n + (s[(v - 20) % 10] || s[v] || s[0]); }

  function court() {
    var sh = D.shots, df = D.def || {zone: {}}, N = D.nteams || 30;
    if (!sh) return '<p class="small dim">No shot data yet.</p>';
    function fill(z) {
      if (S.court === 'player') { var s = sh.zones[z].share; return 'rgba(232,52,78,' + (0.12 + Math.min(1, s / 45) * 0.78).toFixed(2) + ')'; }
      var rk = (df.zone[z] || [0, 15])[1], t = (rk - 1) / (N - 1);
      return t <= 0.33 ? 'rgba(47,212,122,0.70)' : (t >= 0.67 ? 'rgba(232,52,78,0.70)' : 'rgba(150,150,160,0.45)');
    }
    function lab(z) {
      if (S.court === 'player') { var s = sh.zones[z]; return s.share + '%' + (s.fg != null ? ' &middot; ' + s.fg + '% FG' : ''); }
      var rk = df.zone[z]; return rk ? '#' + rk[1] : '-';
    }
    var s = '<svg viewBox="0 0 500 470" class="lab-court" role="img" aria-label="Shooting zones">' +
      '<rect x="0" y="0" width="500" height="470" fill="' + fill('above3') + '"/>' +
      '<path d="M30 0 L30 140 A237.5 237.5 0 0 0 470 140 L470 0 Z" fill="' + fill('mid') + '"/>' +
      '<rect x="0" y="0" width="30" height="140" fill="' + fill('corner3') + '"/><rect x="470" y="0" width="30" height="140" fill="' + fill('corner3') + '"/>' +
      '<rect x="170" y="0" width="160" height="190" fill="' + fill('paint') + '"/>' +
      '<circle cx="250" cy="52" r="45" fill="' + fill('rim') + '"/>' +
      '<g class="lab-lines"><rect x="0" y="0" width="500" height="470"/><path d="M30 0 L30 140 A237.5 237.5 0 0 0 470 140 L470 0"/><rect x="170" y="0" width="160" height="190"/><circle cx="250" cy="190" r="60"/><circle cx="250" cy="52" r="7.5"/><line x1="220" x2="280" y1="40" y2="40"/></g>' +
      '<text x="250" y="60" class="lab-zt">' + lab('rim') + '</text><text x="250" y="150" class="lab-zt">' + lab('paint') + '</text>' +
      '<text x="95" y="230" class="lab-zt">' + lab('mid') + '</text><text x="405" y="230" class="lab-zt">' + lab('mid') + '</text>' +
      '<text x="15" y="80" class="lab-zt lab-zv">' + lab('corner3') + '</text><text x="485" y="80" class="lab-zt lab-zv">' + lab('corner3') + '</text>' +
      '<text x="250" y="420" class="lab-zt">' + lab('above3') + '</text></svg>';
    var leg = S.court === 'player' ? 'His share of points &middot; FG% by zone'
      : esc(D.opp) + ' rank in points allowed (#1 = most) &middot; green soft, red tough';
    return s + '<p class="lab-cleg">' + leg + '</p>';
  }

  function types() {
    var sh = D.shots, df = D.def || {kind: {}};
    if (!sh) return '';
    var k = Object.keys(D.type_label);
    return '<table class="stat-table lab-tt"><thead><tr><th>Type</th><th>His pts/g</th><th>' + esc(D.opp) + ' allows</th></tr></thead><tbody>' +
      k.map(function (x) { var s = sh.kinds[x]; return '<tr><td>' + D.type_label[x] + '</td><td>' + s.ppg + ' <span class="dim small">(' + s.share + '%)</span></td><td>' + rankTxt(df.kind[x]) + '</td></tr>'; }).join('') +
      '</tbody></table>';
  }

  function similar() {
    var r = D.similar.filter(function (g) { return g.ln[S.stat] != null; }); if (S.n) r = r.slice(0, S.n); var n = r.length;
    if (!n) return '<p class="small dim">No ' + esc(D.pos || '') + ' with a ' + LAB[S.stat] + ' line has faced ' + esc(D.opp) + ' yet this season.</p>';
    var over = 0, d = 0, dp = 0;
    r.forEach(function (g) { var l = g.ln[S.stat], v = g.v[S.stat]; if (v > l) over++; d += v - l; dp += l ? (v - l) / l : 0; });
    return '<div class="lab-simtop"><span><b class="' + (d >= 0 ? 'stat-good' : 'stat-bad') + '">' + (d / n >= 0 ? '+' : '') + f1(d / n) + '</b>avg vs line</span>' +
      '<span><b class="' + (dp >= 0 ? 'stat-good' : 'stat-bad') + '">' + (dp >= 0 ? '+' : '') + Math.round(100 * dp / n) + '%</b>avg vs line %</span>' +
      '<span><b>' + Math.round(100 * over / n) + '%</b>went over (' + over + '/' + n + ')</span></div>' +
      '<div class="lab-simwrap"><table class="stat-table"><thead><tr><th>Date</th><th>Team</th><th>Player</th><th>Line</th><th>Result</th><th>Diff</th></tr></thead><tbody>' +
      r.map(function (g) { var l = g.ln[S.stat], v = g.v[S.stat], p = l ? Math.round(100 * (v - l) / l) : 0;
        return '<tr><td>' + g.d.slice(5).replace('-', '/') + '</td><td>' + esc(g.team) + '</td><td>' + esc(g.name) + '</td><td>' + l + '</td><td>' + v + '</td><td><span class="pr-tag ' + (v > l ? 'pr-good' : 'pr-toohigh') + '" style="margin-left:0">' + (p > 0 ? '+' : '') + p + '%</span></td></tr>'; }).join('') +
      '</tbody></table></div>';
  }

  function btn(group, val, label) { return '<button data-g="' + group + '" data-v="' + val + '" class="' + (String(S[group]) === String(val) ? 'active' : '') + '">' + label + '</button>'; }
  function tog(group, label) { return '<button data-t="' + group + '" class="' + (S[group] ? 'active' : '') + '">' + label + '</button>'; }

  function render() {
    var r = rows();
    $('#lab-stat').innerHTML = Object.keys(LAB).map(function (s) { return btn('stat', s, LAB[s]); }).join('');
    $('#lab-filt').innerHTML =
      '<div class="seg seg2">' + btn('n', 5, 'L5') + btn('n', 10, 'L10') + btn('n', 15, 'L15') + btn('n', 0, 'SEASON') + '</div>' +
      '<div class="seg seg2">' + btn('venue', 'all', 'ALL') + btn('venue', 'home', 'HOME') + btn('venue', 'away', 'AWAY') + '</div>' +
      '<div class="seg seg2">' + tog('vs', 'VS ' + esc(D.opp)) + tog('b2b', 'B2B') + btn('margin', 'all', 'ANY MARGIN') + btn('margin', 'won', 'WON BY 10+') + btn('margin', 'lost', 'LOST BY 10+') + '</div>' +
      '<div class="seg seg2">' + btn('min', 0, 'ANY MIN') + btn('min', 25, '25+ MIN') + btn('min', 30, '30+ MIN') + btn('mode', 'avg', 'AVERAGE') + btn('mode', 'med', 'MEDIAN') + '</div>';
    $('#lab-head').innerHTML = head(r);
    $('#lab-chart').innerHTML = r.length ? chart(r) : '<p class="small dim">No games match these filters.</p>';
    $('#lab-stats').innerHTML = stats(r);
    $('#lab-court-tg').innerHTML = btn('court', 'player', 'PLAYER') + btn('court', 'def', 'VS ' + esc(D.opp) + ' DEFENSE');
    $('#lab-court').innerHTML = court();
    $('#lab-types').innerHTML = types();
    $('#lab-sim-h').textContent = 'Similar players vs ' + D.opp + ' (' + (D.pos || '') + ', ' + LAB[S.stat] + ')';
    $('#lab-sim').innerHTML = similar();
  }
  root.addEventListener('click', function (e) {
    var b = e.target.closest('button'); if (!b) return;
    if (b.dataset.g) { var v = b.dataset.v; S[b.dataset.g] = /^\d+$/.test(v) ? +v : v; render(); }
    else if (b.dataset.t) { S[b.dataset.t] = !S[b.dataset.t]; render(); }
  });
  render();
})();
