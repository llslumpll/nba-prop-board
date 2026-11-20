/* Player Lab (update 81, verdict-first design Nov 19): verdict + signals, chart, matchup, similar players. Data: #lab-data JSON. */
(function () {
  var D = JSON.parse(document.getElementById('lab-data').textContent), root = document.getElementById('lab2');
  var LAB = {pts: 'PTS', reb: 'REB', ast: 'AST', fg3m: '3PM', pra: 'PRA', pa: 'P+A', pr: 'P+R', ra: 'R+A'};
  var S = {stat: D.first || 'pts', n: 15, venue: 'all', vs: false, b2b: false, margin: 'all', min: 0, court: 'player', part: '', wo: '', more: false};
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
      if (S.wo) { var mt = (D.mates || []).filter(function (x) { return String(x.pid) === String(S.wo); })[0]; if (mt && mt.missed.indexOf(g.d) < 0) return false; }
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
    } else out.push(['Matchup', 'n', 'see the ' + (S.stat === 'reb' ? 'rebound' : (S.stat === 'ast' ? 'assist' : 'zone')) + ' map below (not in the verdict yet)']);
    var sr = simRows(); if (sr.length >= 4) { var so = sr.filter(function (g) { return g.v[S.stat] > g.ln[S.stat]; }).length / sr.length;
      out.push(['Similar players', ((so - .5) * sgn) > .08 ? 'g' : (((so - .5) * sgn) < -.08 ? 'r' : 'n'), Math.round(so * 100) + '% went over vs ' + esc(D.opp)]); }
    else out.push(['Similar players', 'n', 'too few vs ' + esc(D.opp)]);
    var all = D.log.map(function (g) { return g.min; }), l5 = avg(all.slice(-5)), sz = avg(all);
    if (l5 != null && sz != null) out.push(['Minutes', l5 - sz > 2 ? 'g' : (l5 - sz < -2 ? 'r' : 'n'), 'last 5: ' + f1(l5) + ' vs ' + f1(sz) + ' season']);
    return {list: out, rate: rate, over: over, side: side};
  }

  /* Season record of each verdict and light (data/labsignals, graded every night; price = closing break-even) */
  var KEY = {'Form vs price': 'form', 'Matchup': 'matchup', 'Similar players': 'similar', 'Minutes': 'minutes'};
  function pct(x) { return Math.round(x) + '%'; }
  function lightRec(name, c) {
    var tr = D.track, k = KEY[name], rec = tr && k && (c === 'g' || c === 'r') ? tr.sig[k][c] : null;
    if (!rec || rec.n < 50) return '';
    var d = rec.rate - rec.price;
    return '<em class="v2-rec ' + (d >= 0 ? 'up' : 'dn') + '">When ' + (c === 'g' ? 'green' : 'red') + ' this season: hit ' + pct(rec.rate) + ' vs ' + pct(rec.price) + ' priced</em>';
  }
  function toneRec(tone) {
    var rec = D.track && D.track.tone[tone];
    if (!rec || !rec.n) return '';
    var d = rec.rate - rec.price;
    return '<div class="v2-trk ' + (d >= 0 ? 'up' : 'dn') + '"><b>' + (tone === 'good' ? 'Signals line up' : (tone === 'bad' ? 'Signals point the other way' : 'Mixed signals')) +
      ' has gone ' + rec.w.toLocaleString('en-US') + '-' + rec.l.toLocaleString('en-US') + ' this season</b> (hit ' + rec.rate.toFixed(1) + '% vs ' + rec.price.toFixed(1) + '% priced, ' + (rec.roi > 0 ? '+' : '\u2212') + Math.abs(rec.roi).toFixed(1) + '% per bet). ' +
      (d >= 0 ? 'So far it beats the price.' : 'Not an edge yet: use it as information, not a reason to bet.') + '</div>';
  }

  function verdict(r) {
    var t = T(), sg = signals(r), g = sg.list.filter(function (x) { return x[1] === 'g'; }).length, rd = sg.list.filter(function (x) { return x[1] === 'r'; }).length;
    var tone = g >= 3 && rd === 0 ? 'good' : (rd >= 2 ? 'bad' : 'mixed');
    var head = t.line != null ? '<span class="v2-pick ' + (sg.side === 'OVER' ? 'ov' : 'un') + '">' + sg.side + ' ' + t.line + ' ' + LAB[S.stat] + '</span>' : '<span class="v2-pick nl">No ' + LAB[S.stat] + ' line tonight</span>';
    return '<div class="v2-verdict v2-' + tone + '"><div class="v2-vl">' + head +
      '<div class="v2-nums"><div><b>' + (t.proj != null ? f1(t.proj) : '-') + '</b><span>our projection</span></div><div><b>' + (t.be_over != null ? odds(sg.side === 'OVER' ? t.be_over : 1 - t.be_over) : '-') + '</b><span>price</span></div>' +
      '<div><b>' + (sg.rate == null ? '-' : Math.round(sg.rate * 100) + '%') + '</b><span>hit over, shown games</span></div></div></div>' +
      '<div class="v2-sig">' + sg.list.map(function (x) { return '<div class="v2-s v2-' + x[1] + '"><i></i><b>' + x[0] + '</b><span>' + x[2] + '</span>' + lightRec(x[0], x[1]) + '</div>'; }).join('') + '</div>' +
      '<div class="v2-tone">' + (tone === 'good' ? 'Signals line up' : (tone === 'bad' ? 'Signals point the other way' : 'Mixed signals')) + ' &middot; ' + g + ' green, ' + rd + ' red</div>' + toneRec(tone) + '</div>';
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

  /* Without teammates (update 95): his numbers in games a key teammate missed vs games they played (game level). */
  function woTable() {
    var M = D.mates || []; if (!M.length) return '';
    var st = S.stat, lab = LAB[st];
    function agg(gs) { if (!gs.length) return null; var v = avg(gs.map(function (g) { return g.v[st]; })), mn = avg(gs.map(function (g) { return g.min; })),
      tot = gs.reduce(function (a, g) { return a + g.v[st]; }, 0), tm = gs.reduce(function (a, g) { return a + g.min; }, 0), fga = avg(gs.map(function (g) { return g.fga; }));
      return {n: gs.length, v: v, mn: mn, p36: tm ? 36 * tot / tm : null, fga: fga}; }
    var rowsH = M.map(function (x) {
      var wo = agg(D.log.filter(function (g) { return x.missed.indexOf(g.d) >= 0; })), wi = agg(D.log.filter(function (g) { return x.missed.indexOf(g.d) < 0; }));
      if (!wo || !wi) return '';
      var up = wo.v - wi.v;
      return '<tr class="' + (x.out ? 'v2-outrow' : '') + '"><td>' + esc(x.name) + (x.out ? ' <em class="v2-outt">OUT TONIGHT</em>' : '') + '</td><td><b>' + f1(wo.v) + '</b> <span class="dim">' + wo.n + 'g</span></td><td>' + f1(wi.v) + ' <span class="dim">' + wi.n + 'g</span></td><td class="' + (up > 0.5 ? 'stat-good' : (up < -0.5 ? 'stat-bad' : '')) + '">' + (up >= 0 ? '+' : '') + f1(up) + '</td><td>' + f1(wo.p36) + ' / ' + f1(wi.p36) + '</td><td>' + f1(wo.mn) + ' / ' + f1(wi.mn) + '</td><td>' + f1(wo.fga) + ' / ' + f1(wi.fga) + '</td></tr>';
    }).join('');
    if (!rowsH) return '';
    return '<div class="v2-sub" style="margin-top:12px">Without teammates &middot; ' + lab + ' <span>games he missed vs played, this season</span></div><div class="v2-wot-wrap"><table class="v2-wot"><thead><tr><th>Teammate</th><th>Without</th><th>With</th><th>Diff</th><th>Per 36</th><th>Min</th><th>FGA</th></tr></thead><tbody>' + rowsH + '</tbody></table></div>' +
      '<p class="v2-cap">Per 36 / Min / FGA: without / with. Small samples: a few games without someone can swing a lot.</p>';
  }

  /* Matchup panel (update 91): follows the stat tab. Points = where he scores; Threes = corner vs above-break;
     Assists = the shot his pass set up; Rebounds = where the miss came from. Combos pick a part. */
  var PARTS = {pra: ['pts', 'reb', 'ast'], pa: ['pts', 'ast'], pr: ['pts', 'reb'], ra: ['reb', 'ast']};
  var ZN = {rim: 'At the rim', paint: 'Paint (non-rim)', mid: 'Midrange', corner3: 'Corner 3', above3: 'Above-break 3', ft: 'Free throws'};
  function court(val, lab, only) {
    function f(z) { return (only && only.indexOf(z) < 0) ? 'rgba(120,120,130,.12)' : val(z); }
    function l(z) { return (only && only.indexOf(z) < 0) ? '' : lab(z); }
    return '<svg viewBox="0 0 500 470" class="v2-court"><rect width="500" height="470" fill="' + f('above3') + '"/><path d="M30 0 L30 140 A237.5 237.5 0 0 0 470 140 L470 0 Z" fill="' + f('mid') + '"/><rect width="30" height="140" fill="' + f('corner3') + '"/><rect x="470" width="30" height="140" fill="' + f('corner3') + '"/><rect x="170" width="160" height="190" fill="' + f('paint') + '"/><circle cx="250" cy="52" r="45" fill="' + f('rim') + '"/>' +
      '<g class="v2-cl"><rect width="500" height="470"/><path d="M30 0 L30 140 A237.5 237.5 0 0 0 470 140 L470 0"/><rect x="170" width="160" height="190"/><circle cx="250" cy="190" r="60"/></g>' +
      '<text x="250" y="62" class="v2-zt">' + l('rim') + '</text><text x="250" y="150" class="v2-zt">' + l('paint') + '</text><text x="92" y="235" class="v2-zt">' + l('mid') + '</text><text x="408" y="235" class="v2-zt">' + l('mid') + '</text><text x="16" y="95" class="v2-zt v2-zs">' + l('corner3') + '</text><text x="484" y="95" class="v2-zt v2-zs">' + l('corner3') + '</text><text x="250" y="420" class="v2-zt">' + l('above3') + '</text></svg>';
  }
  function heat(share, top) { return 'rgba(232,52,78,' + (0.12 + Math.min(1, share / (top || 45)) * .8).toFixed(2) + ')'; }
  function rkFill(rk, N) { if (!rk) return 'rgba(150,150,160,.25)'; var t = (rk[1] - 1) / (N - 1); return t <= .33 ? 'rgba(47,212,122,.72)' : (t >= .67 ? 'rgba(232,52,78,.72)' : 'rgba(150,150,160,.45)'); }
  function rkCls(rk, N) { return !rk ? '' : (rk[1] <= 10 ? 'g' : (rk[1] > N - 10 ? 'r' : 'n')); }
  function bar(name, share, rk, N, note) { return '<div class="v2-tb"><span class="v2-tn">' + name + (note ? ' <span class="dim">' + note + '</span>' : '') + '</span><span class="v2-tbar"><i style="width:' + Math.max(2, share) + '%"></i></span><span class="v2-tv">' + share + '%</span><span class="v2-rk v2-' + rkCls(rk, N) + '">' + (rk ? ord(rk[1]) : '-') + '</span></div>'; }
  function toggle(defLabel) { return '<div class="v2-tg"><button data-g="court" data-v="player" class="' + (S.court === 'player' ? 'on' : '') + '">' + defLabel[0] + '</button><button data-g="court" data-v="def" class="' + (S.court === 'def' ? 'on' : '') + '">' + esc(D.opp) + ' defense</button></div>'; }
  function matchup() {
    var N = D.nteams || 30, st = S.stat, parts = PARTS[st], head = '';
    if (parts) { if (parts.indexOf(S.part) < 0) S.part = parts[0]; st = S.part;
      head = '<div class="v2-tabs v2-parts">' + parts.map(function (p) { return '<button data-g="part" data-v="' + p + '" class="' + (S.part === p ? 'on' : '') + '">' + LAB[p] + ' part</button>'; }).join('') + '</div>'; }
    var O = D.other || {}, X = D.odef || {}, title = '<b>Matchup vs ' + esc(D.opp) + ' &middot; ' + LAB[st] + '</b>';
    if (st === 'pts') return head + matchupPts(title);
    if (st === 'fg3m') {
      var t = O.fg3m, dm = X.fg3m || {}, da = X.fg3a || {}, Z3 = ['corner3', 'above3'];
      if (!t || !t.n) return head + '<div class="v2-ch">' + title + '</div><p class="v2-cap">No three-point attempts this season yet.</p>';
      return head + '<div class="v2-ch">' + title + toggle(['His threes']) + '</div>' +
        court(function (z) { return S.court === 'player' ? heat(t.zones[z].share, 70) : rkFill(dm[z], N); },
              function (z) { return S.court === 'player' ? t.zones[z].share + '%' : (dm[z] ? '#' + dm[z][1] : '-'); }, Z3) +
        '<p class="v2-cap">' + (S.court === 'player' ? 'Share of his three-point attempts from each spot' : esc(D.opp) + ' rank in threes made allowed there (#1 = most) &middot; green soft, red tough') + '</p>' +
        '<div class="v2-sub">His threes <span>' + esc(D.opp) + ' rank: made / attempts allowed</span></div>' +
        Z3.map(function (z) { var q = t.zones[z]; return '<div class="v2-tb v2-tb3"><span class="v2-tn">' + ZN[z] + ' <span class="dim">' + q.apg + ' att, ' + (q.fg == null ? '-' : q.fg + '%') + '</span></span><span class="v2-tbar"><i style="width:' + Math.max(2, q.share) + '%"></i></span><span class="v2-tv">' + q.share + '%</span><span class="v2-rk v2-' + rkCls(dm[z], N) + '">' + (dm[z] ? ord(dm[z][1]) : '-') + '</span><span class="v2-rk v2-' + rkCls(da[z], N) + '">' + (da[z] ? ord(da[z][1]) : '-') + '</span></div>'; }).join('');
    }
    var a = O[st], d = X[st] || {};
    if (!a || !a.n) return head + '<div class="v2-ch">' + title + '</div><p class="v2-cap">Not enough ' + (st === 'ast' ? 'assists' : 'rebounds') + ' this season to map yet.</p>';
    var zs = (st === 'reb' ? ['rim', 'paint', 'mid', 'corner3', 'above3', 'ft'] : ['rim', 'paint', 'mid', 'corner3', 'above3']).sort(function (x, y) { return a.zones[y].share - a.zones[x].share; });
    var cap = st === 'ast' ? ['Share of his assists by where the shot he set up was taken', 'rank in assists allowed on shots there']
                           : ['Share of his rebounds by where the missed shot came from', 'rank in rebounds allowed off misses from there'];
    var extra = '';
    if (st === 'reb') { var orb = (X.oreb || {}).all, drb = (X.dreb || {}).all;
      extra = '<div class="v2-tb"><span class="v2-tn">Offensive boards <span class="dim">' + a.off + '% of his</span></span><span class="v2-tbar"><i style="width:' + Math.max(2, a.off) + '%"></i></span><span class="v2-tv">' + (orb ? orb[0] : '-') + '</span><span class="v2-rk v2-' + rkCls(orb, N) + '">' + (orb ? ord(orb[1]) : '-') + '</span></div>' +
              '<div class="v2-tb"><span class="v2-tn">Defensive boards <span class="dim">' + (100 - a.off) + '% of his</span></span><span class="v2-tbar"><i style="width:' + Math.max(2, 100 - a.off) + '%"></i></span><span class="v2-tv">' + (drb ? drb[0] : '-') + '</span><span class="v2-rk v2-' + rkCls(drb, N) + '">' + (drb ? ord(drb[1]) : '-') + '</span></div>'; }
    return head + '<div class="v2-ch">' + title + toggle([st === 'ast' ? 'His assists' : 'His rebounds']) + '</div>' +
      court(function (z) { return S.court === 'player' ? heat(a.zones[z].share) : rkFill(d[z], N); },
            function (z) { return S.court === 'player' ? a.zones[z].share + '%' : (d[z] ? '#' + d[z][1] : '-'); }) +
      '<p class="v2-cap">' + (S.court === 'player' ? cap[0] : esc(D.opp) + ' ' + cap[1] + ' (#1 = most) &middot; green soft, red tough') + '</p>' +
      '<div class="v2-sub">' + (st === 'ast' ? 'Where his passes lead' : 'Where his boards come from') + ' <span>' + esc(D.opp) + ' rank, #1 = allows most</span></div>' +
      zs.map(function (z) { return bar(ZN[z], a.zones[z].share, d[z], N, a.zones[z].pg + '/g'); }).join('') + extra;
  }
  function matchupPts(title) {
    var sh = D.shots, df = D.def || {zone: {}, kind: {}}, N = D.nteams || 30;
    if (!sh) return '';
    var ks = Object.keys(D.type_label).sort(function (a, b) { return sh.kinds[b].share - sh.kinds[a].share; });
    var bars = ks.map(function (k) { return bar(D.type_label[k], sh.kinds[k].share, df.kind[k], N); }).join('');
    return '<div class="v2-ch">' + title + toggle(['His zones']) + '</div>' +
      court(function (z) { return S.court === 'player' ? heat(sh.zones[z].share) : rkFill(df.zone[z], N); },
            function (z) { return S.court === 'player' ? sh.zones[z].share + '%' : (df.zone[z] ? '#' + df.zone[z][1] : '-'); }) +
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
      ((D.mates || []).length ? '<div class="v2-tabs v2-wo"><span class="v2-wol">Without:</span>' + b('wo', '', 'Anyone') + D.mates.map(function (x) { return b('wo', x.pid, esc(x.name.split(' ').slice(-1)[0]) + ' <span class="dim">' + x.missed.length + 'g</span>' + (x.out ? ' <em class="v2-outt">OUT TONIGHT</em>' : '')); }).join('') + '</div>' : '') +
      '<button data-t="more" class="v2-more ' + (S.more ? 'on' : '') + '">More filters' + ((S.venue !== 'all' || S.vs || S.b2b || S.margin !== 'all' || S.min) ? ' &bull;' : '') + '</button>' +
      (S.more ? '<div class="v2-morebox"><div class="v2-tabs">' + b('venue', 'all', 'Home+Away') + b('venue', 'home', 'Home') + b('venue', 'away', 'Away') + '</div><div class="v2-tabs">' + t('vs', 'Only vs ' + esc(D.opp)) + t('b2b', 'Back-to-backs') + '</div><div class="v2-tabs">' + b('margin', 'all', 'Any margin') + b('margin', 'won', 'Won by 10+') + b('margin', 'lost', 'Lost by 10+') + '</div><div class="v2-tabs">' + b('min', 0, 'Any min') + b('min', 25, '25+ min') + b('min', 30, '30+ min') + '</div></div>' : '');
    var ln = T().line, ov = r.filter(function (g) { return ln != null && g.v[S.stat] > ln; }).length;
    $('#v2-chart').innerHTML = '<div class="v2-ch"><b>' + LAB[S.stat] + ', last ' + r.length + ' games' + (ln != null ? ' vs ' + ln : '') + '</b><span class="v2-chs">' + (ln != null ? '<b>' + ov + '/' + r.length + ' over</b> &middot; avg ' + f1(avg(r.map(function (g) { return g.v[S.stat]; }))) : 'avg ' + f1(avg(r.map(function (g) { return g.v[S.stat]; })))) + '</span></div>' + (r.length ? chart(r) : '<p class="v2-cap">No games match these filters.</p>') +
      '<div class="v2-mini">' + [['MIN', 'min'], ['FGA', 'fga'], ['3PA', 'fg3a'], ['FTA', 'fta'], ['USG', 'usg']].map(function (c) { return '<div><b>' + f1(avg(r.map(function (g) { return g[c[1]]; }))) + '</b><span>' + c[0] + '</span></div>'; }).join('') + '</div>';
    var wt = woTable(); if (wt) $('#v2-chart').innerHTML += wt;
    $('#v2-match').innerHTML = matchup();
    $('#v2-sim').innerHTML = similar();
  }
  root.addEventListener('click', function (e) { var x = e.target.closest('button'); if (!x) return;
    if (x.dataset.g) { var v = x.dataset.v; S[x.dataset.g] = /^\d+$/.test(v) ? +v : v; render(); } else if (x.dataset.t) { S[x.dataset.t] = !S[x.dataset.t]; render(); } });
  render();
})();
