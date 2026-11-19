/* Player Lab (update 81, verdict-first design Nov 19): verdict + signals, chart, matchup, similar players. Data: #lab-data JSON. */
(function () {
  var D = JSON.parse(document.getElementById('lab-data').textContent), root = document.getElementById('lab2');
  var LAB = {pts: 'PTS', reb: 'REB', ast: 'AST', fg3m: '3PM', pra: 'PRA', pa: 'P+A', pr: 'P+R', ra: 'R+A'};
  var S = {stat: D.first || 'pts', n: 15, venue: 'all', vs: false, b2b: false, margin: 'all', min: 0, court: 'player', more: false};
  function $(q) { return root.querySelector(q); }
  function esc(s) { return String(s).replace(/[&<>"]/g, function (c) { return {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}[c]; }); }
  function avg(a) { return a.length ? a.reduce(function (x, y) { return x + y; }, 0) / a.length : null; }
  function f1(x) { return x == null ? '-' : (Math.round(x * 10) / 10).toFixed(1); }
  function ord(n) { var s = ['th', 'st', 'nd', 'rd'], v = n % 100; return n + (s[(v - 20) % 10] || s[v] || s[0]); }
  function odds(p) { if (!p) return '-'; return p >= .5 ? '-' + Math.round(100 * p / (1 - p)) : '+' + Math.round(100 * (1 - p) / p); }
  function rows() {
    var r = D.log.filter(function (g) {
      if (S.venue === 'home' && !g.home) return false; if (S.venue === 'away' && g.home) return false;
      if (S.vs && g.opp !== D.opp_raw) return false; if (S.b2b && !g.b2b) return false;
      if (S.margin === 'won' && g.m < 10) return false; if (S.margin === 'lost' && g.m > -10) return false;
      return g.min >= S.min; });
    return S.n ? r.slice(-S.n) : r;
  }
  function T() { return D.tonight[S.stat] || {}; }
  function simRows() { var r = D.similar.filter(function (g) { return g.ln[S.stat] != null; }); return S.n ? r.slice(0, S.n) : r; }

  function signals(r) {
    var t = T(), ln = t.line, imp = t.be_over, out = [];
    var over = r.filter(function (g) { return ln != null && g.v[S.stat] > ln; }).length, rate = r.length && ln != null ? over / r.length : null;
    var side = t.call === 'UNDER' ? 'UNDER' : 'OVER', sgn = side === 'OVER' ? 1 : -1;
    if (rate != null && imp != null) { var d = (rate - imp) * sgn; out.push(['Form vs price', d > .05 ? 'g' : (d < -.05 ? 'r' : 'n'), Math.round(rate * 100) + '% over vs ' + Math.round(imp * 100) + '% priced']); }
    else out.push(['Form vs price', 'n', 'no line tonight']);
    var scoring = /pts|fg3m|pra|pa|pr/.test(S.stat) && S.stat !== 'ra';
    if (scoring && D.shots && D.def) {
      var N = D.nteams || 30, sc = 0, w = 0;
      Object.keys(D.type_label).forEach(function (k) { var sh = D.shots.kinds[k].share / 100, rk = (D.def.kind[k] || [0, 15])[1]; sc += sh * (N + 1 - rk) / N; w += sh; });
      sc = w ? sc / w : .5; var good = sc * sgn > .55 * sgn || (sgn < 0 && sc < .45);
      out.push(['Matchup', sgn > 0 ? (sc > .55 ? 'g' : (sc < .45 ? 'r' : 'n')) : (sc < .45 ? 'g' : (sc > .55 ? 'r' : 'n')), esc(D.opp) + (sc > .55 ? ' soft' : (sc < .45 ? ' tough' : ' average')) + ' where he scores']);
    } else out.push(['Matchup', 'n', 'zones apply to scoring stats']);
    var sr = simRows(); if (sr.length >= 4) { var so = sr.filter(function (g) { return g.v[S.stat] > g.ln[S.stat]; }).length / sr.length;
      out.push(['Similar players', ((so - .5) * sgn) > .08 ? 'g' : (((so - .5) * sgn) < -.08 ? 'r' : 'n'), Math.round(so * 100) + '% went over vs ' + esc(D.opp)]); }
    else out.push(['Similar players', 'n', 'too few vs ' + esc(D.opp)]);
    var all = D.log.map(function (g) { return g.min; }), l5 = avg(all.slice(-5)), sz = avg(all);
    if (l5 != null && sz != null) out.push(['Minutes', l5 - sz > 2 ? 'g' : (l5 - sz < -2 ? 'r' : 'n'), 'last 5: ' + f1(l5) + ' vs ' + f1(sz) + ' season']);
    return {list: out, rate: rate, over: over, side: side};
  }

  function verdict(r) {
    var t = T(), sg = signals(r), g = sg.list.filter(function (x) { return x[1] === 'g'; }).length, rd = sg.list.filter(function (x) { return x[1] === 'r'; }).length;
    var tone = g >= 3 && rd === 0 ? 'good' : (rd >= 2 ? 'bad' : 'mixed');
    var head = t.line != null ? '<span class="v2-pick ' + (sg.side === 'OVER' ? 'ov' : 'un') + '">' + sg.side + ' ' + t.line + ' ' + LAB[S.stat] + '</span>' : '<span class="v2-pick nl">No ' + LAB[S.stat] + ' line tonight</span>';
    return '<div class="v2-verdict v2-' + tone + '"><div class="v2-vl">' + head +
      '<div class="v2-nums"><div><b>' + (t.proj != null ? f1(t.proj) : '-') + '</b><span>our projection</span></div><div><b>' + (t.be_over != null ? odds(sg.side === 'OVER' ? t.be_over : 1 - t.be_over) : '-') + '</b><span>price</span></div>' +
      '<div><b>' + (sg.rate == null ? '-' : Math.round(sg.rate * 100) + '%') + '</b><span>hit over, shown games</span></div></div></div>' +
      '<div class="v2-sig">' + sg.list.map(function (x) { return '<div class="v2-s v2-' + x[1] + '"><i></i><b>' + x[0] + '</b><span>' + x[2] + '</span></div>'; }).join('') + '</div>' +
      '<div class="v2-tone">' + (tone === 'good' ? 'Signals line up' : (tone === 'bad' ? 'Signals point the other way' : 'Mixed signals')) + ' &middot; ' + g + ' green, ' + rd + ' red</div></div>';
  }

  function chart(r) {
    var ln = T().line, W = 820, H = 300, pad = 34, step = (W - pad * 2) / Math.max(1, r.length), bw = Math.max(10, Math.min(40, step - 8));
    var mx = Math.max.apply(null, [ln || 0].concat(r.map(function (g) { return g.v[S.stat]; }))) * 1.15 || 1;
    var y = function (v) { return H - 40 - (H - 70) * v / mx; }, s = '<svg viewBox="0 0 ' + W + ' ' + H + '" class="v2-svg">';
    r.forEach(function (g, i) { var v = g.v[S.stat], x = pad + i * step + (step - bw) / 2, ov = ln != null && v > ln, h = Math.max(3, (H - 40) - y(v));
      s += '<rect x="' + x + '" y="' + (H - 40 - h) + '" width="' + bw + '" height="' + h + '" rx="3" fill="' + (ln == null ? '#8a8f98' : ov ? '#2fd47a' : '#e8344e') + '"/>';
      s += '<text x="' + (x + bw / 2) + '" y="' + (H - 44 - h) + '" class="v2-val">' + v + '</text><text x="' + (x + bw / 2) + '" y="' + (H - 22) + '" class="v2-ax">' + esc(g.opp) + '</text><text x="' + (x + bw / 2) + '" y="' + (H - 6) + '" class="v2-ax2">' + g.d.slice(5).replace('-', '/') + '</text>';
      if (g.ln[S.stat] != null) s += '<line x1="' + (x - 2) + '" x2="' + (x + bw + 2) + '" y1="' + y(g.ln[S.stat]) + '" y2="' + y(g.ln[S.stat]) + '" class="v2-tick"/>'; });
    if (ln != null) s += '<line x1="' + pad + '" x2="' + (W - pad) + '" y1="' + y(ln) + '" y2="' + y(ln) + '" class="v2-line"/><text x="2" y="' + (y(ln) + 5) + '" class="v2-lnl">' + ln + '</text>';
    return s + '</svg>';
  }

  function matchup() {
    var sh = D.shots, df = D.def || {zone: {}, kind: {}}, N = D.nteams || 30;
    if (!sh) return '';
    function fill(z) { if (S.court === 'player') return 'rgba(232,52,78,' + (0.12 + Math.min(1, sh.zones[z].share / 45) * .8).toFixed(2) + ')';
      var t = ((df.zone[z] || [0, 15])[1] - 1) / (N - 1); return t <= .33 ? 'rgba(47,212,122,.72)' : (t >= .67 ? 'rgba(232,52,78,.72)' : 'rgba(150,150,160,.45)'); }
    function lab(z) { return S.court === 'player' ? sh.zones[z].share + '%' : (df.zone[z] ? '#' + df.zone[z][1] : '-'); }
    var court = '<svg viewBox="0 0 500 470" class="v2-court"><rect width="500" height="470" fill="' + fill('above3') + '"/><path d="M30 0 L30 140 A237.5 237.5 0 0 0 470 140 L470 0 Z" fill="' + fill('mid') + '"/><rect width="30" height="140" fill="' + fill('corner3') + '"/><rect x="470" width="30" height="140" fill="' + fill('corner3') + '"/><rect x="170" width="160" height="190" fill="' + fill('paint') + '"/><circle cx="250" cy="52" r="45" fill="' + fill('rim') + '"/>' +
      '<g class="v2-cl"><rect width="500" height="470"/><path d="M30 0 L30 140 A237.5 237.5 0 0 0 470 140 L470 0"/><rect x="170" width="160" height="190"/><circle cx="250" cy="190" r="60"/></g>' +
      '<text x="250" y="62" class="v2-zt">' + lab('rim') + '</text><text x="250" y="150" class="v2-zt">' + lab('paint') + '</text><text x="92" y="235" class="v2-zt">' + lab('mid') + '</text><text x="408" y="235" class="v2-zt">' + lab('mid') + '</text><text x="16" y="95" class="v2-zt v2-zs">' + lab('corner3') + '</text><text x="484" y="95" class="v2-zt v2-zs">' + lab('corner3') + '</text><text x="250" y="420" class="v2-zt">' + lab('above3') + '</text></svg>';
    var ks = Object.keys(D.type_label).sort(function (a, b) { return sh.kinds[b].share - sh.kinds[a].share; });
    var bars = ks.map(function (k) { var s = sh.kinds[k], rk = df.kind[k], c = !rk ? '' : (rk[1] <= 10 ? 'g' : (rk[1] > N - 10 ? 'r' : 'n'));
      return '<div class="v2-tb"><span class="v2-tn">' + D.type_label[k] + '</span><span class="v2-tbar"><i style="width:' + Math.max(2, s.share) + '%"></i></span><span class="v2-tv">' + s.share + '%</span><span class="v2-rk v2-' + c + '">' + (rk ? ord(rk[1]) : '-') + '</span></div>'; }).join('');
    return '<div class="v2-ch"><b>Matchup vs ' + esc(D.opp) + '</b><div class="v2-tg"><button data-g="court" data-v="player" class="' + (S.court === 'player' ? 'on' : '') + '">His zones</button><button data-g="court" data-v="def" class="' + (S.court === 'def' ? 'on' : '') + '">' + esc(D.opp) + ' defense</button></div></div>' + court +
      '<p class="v2-cap">' + (S.court === 'player' ? 'Share of his points from each zone' : esc(D.opp) + ' rank in points allowed (#1 = most) &middot; green soft, red tough') + '</p>' +
      '<div class="v2-sub">Where his points come from <span>' + esc(D.opp) + ' rank, #1 = allows most</span></div>' + bars;
  }

  function similar() {
    var r = simRows(), n = r.length; if (!n) return '<p class="v2-cap">No ' + esc(D.pos || '') + ' with a ' + LAB[S.stat] + ' line has faced ' + esc(D.opp) + ' yet.</p>';
    var over = r.filter(function (g) { return g.v[S.stat] > g.ln[S.stat]; }).length, d = avg(r.map(function (g) { return g.v[S.stat] - g.ln[S.stat]; }));
    return '<div class="v2-ch"><b>Similar ' + esc(D.pos || '') + 's vs ' + esc(D.opp) + ' &middot; ' + LAB[S.stat] + '</b><span class="v2-chs"><b class="' + (over / n >= .5 ? 'stat-good' : 'stat-bad') + '">' + over + '/' + n + ' over</b> &middot; avg ' + (d >= 0 ? '+' : '') + f1(d) + ' vs line</span></div>' +
      '<div class="v2-simwrap"><table class="stat-table"><thead><tr><th>Date</th><th>Player</th><th>Line</th><th>Result</th><th></th></tr></thead><tbody>' +
      r.map(function (g) { var l = g.ln[S.stat], v = g.v[S.stat]; return '<tr><td>' + g.d.slice(5).replace('-', '/') + '</td><td>' + esc(g.name) + ' <span class="dim">' + esc(g.team) + '</span></td><td>' + l + '</td><td>' + v + '</td><td><span class="v2-dot ' + (v > l ? 'g' : 'r') + '">' + (v > l ? 'OVER' : 'UNDER') + '</span></td></tr>'; }).join('') + '</tbody></table></div>';
  }

  function render() {
    var r = rows(), b = function (g, v, l) { return '<button data-g="' + g + '" data-v="' + v + '" class="' + (String(S[g]) === String(v) ? 'on' : '') + '">' + l + '</button>'; },
        t = function (g, l) { return '<button data-t="' + g + '" class="' + (S[g] ? 'on' : '') + '">' + l + '</button>'; };
    $('#v2-verdict').innerHTML = verdict(r);
    $('#v2-ctrl').innerHTML = '<div class="v2-tabs">' + Object.keys(LAB).map(function (s) { return b('stat', s, LAB[s]); }).join('') + '</div>' +
      '<div class="v2-tabs">' + b('n', 5, 'L5') + b('n', 10, 'L10') + b('n', 15, 'L15') + b('n', 0, 'Season') + '</div>' +
      '<button data-t="more" class="v2-more ' + (S.more ? 'on' : '') + '">More filters' + ((S.venue !== 'all' || S.vs || S.b2b || S.margin !== 'all' || S.min) ? ' &bull;' : '') + '</button>' +
      (S.more ? '<div class="v2-morebox"><div class="v2-tabs">' + b('venue', 'all', 'Home+Away') + b('venue', 'home', 'Home') + b('venue', 'away', 'Away') + '</div><div class="v2-tabs">' + t('vs', 'Only vs ' + esc(D.opp)) + t('b2b', 'Back-to-backs') + '</div><div class="v2-tabs">' + b('margin', 'all', 'Any margin') + b('margin', 'won', 'Won by 10+') + b('margin', 'lost', 'Lost by 10+') + '</div><div class="v2-tabs">' + b('min', 0, 'Any min') + b('min', 25, '25+ min') + b('min', 30, '30+ min') + '</div></div>' : '');
    var ln = T().line, ov = r.filter(function (g) { return ln != null && g.v[S.stat] > ln; }).length;
    $('#v2-chart').innerHTML = '<div class="v2-ch"><b>' + LAB[S.stat] + ', last ' + r.length + ' games' + (ln != null ? ' vs ' + ln : '') + '</b><span class="v2-chs">' + (ln != null ? '<b>' + ov + '/' + r.length + ' over</b> &middot; avg ' + f1(avg(r.map(function (g) { return g.v[S.stat]; }))) : 'avg ' + f1(avg(r.map(function (g) { return g.v[S.stat]; })))) + '</span></div>' + (r.length ? chart(r) : '<p class="v2-cap">No games match these filters.</p>') +
      '<div class="v2-mini">' + [['MIN', 'min'], ['FGA', 'fga'], ['3PA', 'fg3a'], ['FTA', 'fta'], ['USG', 'usg']].map(function (c) { return '<div><b>' + f1(avg(r.map(function (g) { return g[c[1]]; }))) + '</b><span>' + c[0] + '</span></div>'; }).join('') + '</div>';
    $('#v2-match').innerHTML = matchup();
    $('#v2-sim').innerHTML = similar();
  }
  root.addEventListener('click', function (e) { var x = e.target.closest('button'); if (!x) return;
    if (x.dataset.g) { var v = x.dataset.v; S[x.dataset.g] = /^\d+$/.test(v) ? +v : v; render(); } else if (x.dataset.t) { S[x.dataset.t] = !S[x.dataset.t]; render(); } });
  render();
})();
