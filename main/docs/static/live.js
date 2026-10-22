/* Prop Edge NBA live tracker (Games -> LIVE).
   Runs in the visitor's browser: pulls ESPN's public scoreboard + game summary
   every 30 s while the LIVE tab is open, and tracks tonight's frozen picks
   (embedded in the page) against the live box score. Nothing is stored. */
(function () {
  var el = document.getElementById('live-data');
  var root = document.getElementById('live-root');
  var chips = document.getElementById('live-chips');
  if (!el || !root) return;
  var D = JSON.parse(el.textContent || '{}');
  var API = 'https://site.api.espn.com/apis/site/v2/sports/basketball/nba/';
  var REFRESH = 30000;
  var sb = {};            // gid -> scoreboard status
  var seen = {};          // gid -> {playId: true} already shown (only newer baskets animate)
  var lastScore = {};     // gid -> {as, hs} at the previous refresh
  var sel = null, timer = null, busy = false, lastErr = null;
  var COMBO = {pra: ['pts', 'reb', 'ast'], pr: ['pts', 'reb'], pa: ['pts', 'ast'], ra: ['reb', 'ast']};
  var LABEL = {pts: 'PTS', reb: 'REB', ast: 'AST', fg3m: '3PM', pra: 'PRA', pr: 'P+R', pa: 'P+A', ra: 'R+A'};

  function esc(s) { return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) { return {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]; }); }
  function visible() { var p = root.closest('[data-tab-panel]'); return !p || !p.classList.contains('hidden'); }
  function getJSON(url) { return fetch(url, {cache: 'no-store'}).then(function (r) { if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); }); }

  function parseScoreboard(j) {
    (j.events || []).forEach(function (e) {
      var c = (e.competitions || [])[0] || {}, st = (c.status || e.status || {}), ty = st.type || {};
      var h = {}, a = {};
      (c.competitors || []).forEach(function (t) { if (t.homeAway === 'home') h = t; else a = t; });
      sb[String(e.id)] = {state: ty.state || 'pre', detail: ty.shortDetail || ty.detail || '', period: st.period || 0,
        clock: st.displayClock || '', hs: +(h.score || 0), as: +(a.score || 0)};
    });
  }
  function elapsedFrac(s) {
    if (!s || s.state === 'pre') return 0;
    if (s.state === 'post') return 1;
    var p = s.period || 1, parts = String(s.clock || '0:00').split(':'), left = (+parts[0] || 0) + (+parts[1] || 0) / 60;
    var played = p <= 4 ? (p - 1) * 12 + (12 - left) : 48 + (p - 5) * 5 + (5 - left);
    return Math.max(0.01, Math.min(1, played / 48));
  }

  function parseBox(sum) {
    var players = {}, teams = [];
    ((sum.boxscore || {}).players || []).forEach(function (tb) {
      var abbr = ((tb.team || {}).abbreviation) || '';
      var st = (tb.statistics || [])[0] || {}, keys = st.keys || [], names = st.names || st.labels || [];
      function idx(k, n) { var i = keys.indexOf(k); return i >= 0 ? i : names.indexOf(n); }
      var iMin = idx('minutes', 'MIN'), iPts = idx('points', 'PTS'), iReb = idx('rebounds', 'REB'), iAst = idx('assists', 'AST'),
        i3 = idx('threePointFieldGoalsMade-threePointFieldGoalsAttempted', '3PT');
      var rows = [];
      (st.athletes || []).forEach(function (a) {
        var s = a.stats || [], id = String((a.athlete || {}).id || '');
        var v = {pts: +s[iPts] || 0, reb: +s[iReb] || 0, ast: +s[iAst] || 0, fg3m: +(String(s[i3] || '0').split('-')[0]) || 0,
          min: +s[iMin] || 0, dnp: !!a.didNotPlay || !s.length, name: (a.athlete || {}).displayName || '', starter: !!a.starter, team: abbr};
        players[id] = v; rows.push(v);
      });
      teams.push({abbr: abbr, rows: rows});
    });
    return {players: players, teams: teams};
  }
  function teamSides(sum, g) {
    var m = {};
    (((sum.header || {}).competitions || [])[0] || {}).competitors && sum.header.competitions[0].competitors.forEach(function (c) {
      m[String((c.team || {}).id)] = c.homeAway;
    });
    ((sum.boxscore || {}).players || []).forEach(function (tb, i) {
      var id = String(((tb.team || {}).id) || ''), ab = (tb.team || {}).abbreviation;
      if (id && !m[id]) m[id] = ab === g.home ? 'home' : ab === g.away ? 'away' : (i === 1 ? 'home' : 'away');
    });
    return m;
  }
  function val(v, stat) { return COMBO[stat] ? COMBO[stat].reduce(function (t, k) { return t + v[k]; }, 0) : v[stat]; }

  function pickStatus(p, v, s, frac) {
    if (!v) return {cls: 'pend', txt: s && s.state !== 'pre' ? 'not in box yet' : 'not started', cur: null};
    if (v.dnp && (!s || s.state === 'post')) return {cls: 'void', txt: 'VOID (DNP)', cur: 0};
    var cur = val(v, p.stat), over = p.call === 'OVER', post = s && s.state === 'post';
    var pace = (v.min > 0 && p.min) ? cur / v.min * p.min : (frac > 0.05 ? cur / frac : null);
    if (post) {
      if (cur === p.line) return {cls: 'void', txt: 'PUSH', cur: cur, pace: pace};
      var win = over ? cur > p.line : cur < p.line;
      return {cls: win ? 'hit' : 'miss', txt: win ? '✔ HIT' : '✘ MISS', cur: cur, pace: pace};
    }
    if (over && cur > p.line) return {cls: 'hit', txt: '✔ HIT', cur: cur, pace: pace};
    if (!over && cur > p.line) return {cls: 'miss', txt: '✘ LOST', cur: cur, pace: pace};
    var good = pace == null ? null : (over ? pace > p.line : pace < p.line);
    var txt = over ? ('needs ' + (Math.floor(p.line) + 1 - cur)) : ((Math.ceil(p.line) - 1 - cur) + ' to spare');
    return {cls: good == null ? 'pend' : (good ? 'ontrack' : 'offtrack'), txt: txt, cur: cur, pace: pace};
  }
  function gameStatus(t, s, frac) {
    if (!s || s.state === 'pre') return {cls: 'pend', txt: 'not started', now: ''};
    var hs = s.hs, as = s.as, post = s.state === 'post', win, now;
    if (t.market === 'moneyline') { var lead = t.pick === t.home ? hs - as : as - hs; win = lead > 0; now = (lead >= 0 ? '+' : '') + lead; }
    else if (t.market === 'spread') { var m = (hs - as + t.line) * (t.pick === t.home ? 1 : -1); win = m > 0; now = (m >= 0 ? '+' : '') + m + ' vs line'; }
    else { var tot = hs + as, pace = frac > 0.05 ? Math.round(tot / frac) : null; win = t.pick === 'OVER' ? tot > t.line : tot < t.line;
      if (!post && t.pick === 'UNDER' && tot > t.line) return {cls: 'miss', txt: '✘ LOST', now: tot};
      if (!post && t.pick === 'OVER' && tot > t.line) return {cls: 'hit', txt: '✔ HIT', now: tot};
      now = tot + (pace && !post ? ' (pace ' + pace + ')' : ''); if (!post) win = t.pick === 'OVER' ? pace > t.line : pace < t.line; }
    if (post) return {cls: win ? 'hit' : 'miss', txt: win ? '✔ HIT' : '✘ MISS', now: now};
    return {cls: win ? 'ontrack' : 'offtrack', txt: win ? 'on track' : 'behind', now: now};
  }

  function renderChips() {
    chips.innerHTML = (D.games || []).map(function (g) {
      var s = sb[g.gid], sub = !s ? g.tip : s.state === 'pre' ? g.tip : (g.away + ' ' + s.as + '-' + s.hs + ' ' + g.home + ' · ' + s.detail);
      var live = s && s.state === 'in';
      return '<button class="chip' + (g.gid === sel ? ' active' : '') + '" type="button" data-lg="' + g.gid + '">' + (live ? '<i class="live-dot"></i>' : '') +
        esc(g.away) + ' @ ' + esc(g.home) + '<span class="chip-sub">' + esc(sub) + '</span></button>';
    }).join('');
  }

  function render(sum) {
    var g = (D.games || []).filter(function (x) { return x.gid === sel; })[0];
    if (!g) { root.innerHTML = '<div class="chart-empty">No games on this slate.</div>'; return; }
    var s = sb[g.gid] || {state: 'pre', detail: g.tip, hs: 0, as: 0}, frac = elapsedFrac(s);
    var box = sum ? parseBox(sum) : {players: {}, teams: []};
    var h = '<div class="lv-head" style="--ca:' + g.ac + ';--ch:' + g.hc + '"><div class="lv-team"><b>' + esc(g.away) + '</b><span class="lv-score">' + (s.state === 'pre' ? '–' : s.as) + '</span></div>' +
      '<div class="lv-mid">' + (s.state === 'in' ? '<span class="st st-live"><i></i>LIVE</span>' : s.state === 'post' ? '<span class="st st-final">FINAL</span>' : '<span class="st">' + esc(g.tip) + '</span>') +
      '<div class="lv-clock">' + esc(s.state === 'pre' ? 'starts ' + g.tip : s.detail) + '</div></div>' +
      '<div class="lv-team lv-home"><span class="lv-score">' + (s.state === 'pre' ? '–' : s.hs) + '</span><b>' + esc(g.home) + '</b></div></div>';
    if (lastErr) h += '<div class="notice compact audit-bad"><strong>Live data unavailable right now</strong> (' + esc(lastErr) + '). Follow it on <a href="https://www.espn.com/nba/game/_/gameId/' + g.gid + '" target="_blank" rel="noopener">ESPN</a>; this retries every 30 seconds.</div>';
    // game picks
    var gp = (D.gpicks || {})[g.gid] || [];
    if (gp.length) {
      h += '<h3 class="gc-h">Our game picks</h3><div class="table-wrap"><div class="table-scroll"><table class="stat-table lvt"><thead><tr><th>Market</th><th>Pick</th><th class="num">Chance</th><th class="num">Now</th><th>Status</th></tr></thead><tbody>';
      gp.forEach(function (t) { var st = gameStatus(t, s, frac);
        h += '<tr class="lv-' + st.cls + '"><td>' + t.market.toUpperCase() + (t.star ? ' <span class="star">★</span>' : '') + '</td><td><span class="pp-pick pp-' + (t.pick === 'UNDER' ? 'under' : 'over') + '">' + esc(t.label) + '</span></td><td class="num">' + Math.round(t.chance * 100) + '%</td><td class="num">' + esc(st.now) + '</td><td><span class="res res-' + st.cls + '">' + esc(st.txt) + '</span></td></tr>'; });
      h += '</tbody></table></div></div>';
    }
    // player picks
    var pp = ((D.picks || {})[g.gid] || []).slice();
    if (pp.length) {
      var rows = pp.map(function (p) { return {p: p, st: pickStatus(p, box.players[p.pid], s, frac)}; });
      var order = {hit: 0, ontrack: 1, offtrack: 2, pend: 3, miss: 4, void: 5};
      rows.sort(function (a, b) { return (b.p.star - a.p.star) || (order[a.st.cls] - order[b.st.cls]) || (b.p.chance - a.p.chance); });
      var cnt = {hit: 0, miss: 0, ontrack: 0, offtrack: 0};
      rows.forEach(function (r) { if (cnt[r.st.cls] != null) cnt[r.st.cls]++; });
      h += '<h3 class="gc-h">Our player picks <span class="lv-count"><span class="s-over">' + cnt.hit + ' hit</span> · <span class="s-under">' + cnt.miss + ' lost</span> · ' + cnt.ontrack + ' on track · ' + cnt.offtrack + ' behind</span></h3>' +
        '<div class="chips lv-filter"><button class="chip active" type="button" data-lf="star">★ BEST 5 / VALUE</button><button class="chip" type="button" data-lf="all">ALL ' + rows.length + '</button></div>' +
        '<div class="table-wrap"><div class="table-scroll lv-scroll"><table class="stat-table lvt"><thead><tr><th>Player</th><th>Pick</th><th class="num">Now</th><th class="num">Pace</th><th>Status</th></tr></thead><tbody>';
      rows.forEach(function (r) { var p = r.p, st = r.st;
        h += '<tr class="lv-' + st.cls + (p.star ? ' lv-star' : ' lv-nostar') + '"><td><a class="plink" href="p-' + esc(p.pid) + '.html">' + esc(p.player) + '</a>' + (p.star ? ' <span class="star">★</span>' : '') + '<div class="player-sub">' + esc(p.team) + ' · ' + (LABEL[p.stat] || p.stat) + (box.players[p.pid] ? ' · ' + box.players[p.pid].min + ' min' : '') + '</div></td>' +
          '<td><span class="pp-pick pp-' + p.call.toLowerCase() + '">' + p.call + ' ' + p.line + '</span> <span class="pp-ch">' + Math.round(p.chance * 100) + '%</span></td>' +
          '<td class="num"><b class="day-actual">' + (st.cur == null ? '–' : st.cur) + '</b></td><td class="num">' + (st.pace == null || s.state === 'post' ? '–' : st.pace.toFixed(1)) + '</td>' +
          '<td><span class="res res-' + st.cls + '">' + esc(st.txt) + '</span></td></tr>'; });
      h += '</tbody></table></div></div>';
    }
    // box score
    if (box.teams.length) {
      h += '<h3 class="gc-h">Box score</h3><div class="two-col">';
      box.teams.forEach(function (t) {
        h += '<div><div class="gc-team">' + esc(t.abbr) + '</div><div class="table-scroll"><table class="stat-table teamt"><thead><tr><th>Player</th><th class="num">Min</th><th class="num">Pts</th><th class="num">Reb</th><th class="num">Ast</th><th class="num">3PM</th></tr></thead><tbody>';
        t.rows.filter(function (v) { return !v.dnp; }).forEach(function (v) { h += '<tr><td>' + esc(v.name) + (v.starter ? '' : ' <span class="dim small">bench</span>') + '</td><td class="num">' + v.min + '</td><td class="num"><b>' + v.pts + '</b></td><td class="num">' + v.reb + '</td><td class="num">' + v.ast + '</td><td class="num">' + v.fg3m + '</td></tr>'; });
        h += '</tbody></table></div></div>';
      });
      h += '</div>';
    } else if (s.state !== 'pre') { h += '<p class="small dim">Box score not available yet.</p>'; }
    // play by play: shooter's name bold in team color, +N badge on made baskets, new baskets animate
    var plays = (sum && sum.plays) || [];
    var sides = sum ? teamSides(sum, g) : {};
    var names = {}; Object.keys(box.players).forEach(function (id) { names[id] = box.players[id].name; });
    var firstLoad = !seen[g.gid]; seen[g.gid] = seen[g.gid] || {};
    var newBaskets = [];
    if (plays.length) {
      h += '<h3 class="gc-h">Play by play</h3><div class="lv-plays">';
      plays.slice(-40).reverse().forEach(function (pl, idx) {
        var pid = String(pl.id || pl.sequenceNumber || (((pl.period || {}).number || 0) + '-' + ((pl.clock || {}).displayValue || '') + '-' + idx));
        var isNew = !firstLoad && !seen[g.gid][pid];
        seen[g.gid][pid] = true;
        var q = (pl.period || {}).number || '', clk = (pl.clock || {}).displayValue || '';
        var side = sides[String((pl.team || {}).id)] || '', col = side === 'home' ? g.hc : side === 'away' ? g.ac : '';
        var who = ((pl.participants || [])[0] || {}).athlete || {}, nm = names[String(who.id)] || who.displayName || '';
        var txt = esc(pl.text || '');
        var ncol = col ? ' style="color:color-mix(in srgb, ' + col + ' 55%, #ffffff)"' : '';  // lightened so dark team colors stay readable
        if (nm && txt.indexOf(esc(nm)) >= 0) txt = txt.replace(esc(nm), '<b class="lv-who"' + ncol + '>' + esc(nm) + '</b>');
        else if (nm) txt = '<b class="lv-who"' + ncol + '>' + esc(nm) + '</b> ' + txt;
        var pts = pl.scoringPlay ? (+pl.scoreValue || 0) : 0;
        var badge = pts ? '<span class="lv-pts lv-pts-' + pts + '"' + (col ? ' style="--tc:' + col + '"' : '') + '>+' + pts + '</span>' : '<span class="lv-pts lv-pts-0"></span>';
        if (isNew && pts) newBaskets.push({side: side, pts: pts});
        h += '<div class="lv-play' + (pts ? ' lv-score-play' : '') + (isNew ? ' lv-new' : '') + '"' + (col && pts ? ' style="--tc:' + col + '"' : '') + '>' +
          '<span class="lv-pc">Q' + q + ' ' + esc(clk) + '</span>' + badge + '<span class="lv-pt">' + txt + '</span>' +
          '<span class="lv-ps">' + (pl.awayScore != null ? pl.awayScore + '-' + pl.homeScore : '') + '</span></div>';
      });
      h += '</div>';
    } else if (s.state !== 'pre') { h += '<p class="small dim">Play-by-play not available yet.</p>'; }
    h += '<p class="small dim lv-foot">Live data from ESPN, refreshed every 30 s. "Pace" = where the stat ends up at this rate over the minutes we projected. Final grades on History are from the official box score the next morning.</p>';
    var keepAll = root.querySelector('.lv-filter [data-lf="all"].active');
    root.innerHTML = h;
    applyFilter(keepAll ? 'all' : 'star');
    var prev = lastScore[g.gid];
    if (prev && !newBaskets.length && s.state === 'in') {  // no play feed: fall back to the score change
      if (s.as > prev.as) newBaskets.push({side: 'away', pts: Math.min(3, s.as - prev.as)});
      if (s.hs > prev.hs) newBaskets.push({side: 'home', pts: Math.min(3, s.hs - prev.hs)});
    }
    lastScore[g.gid] = {as: s.as, hs: s.hs};
    newBaskets.slice(0, 6).forEach(function (b, i) { setTimeout(function () { popBasket(b, g); }, i * 450); });
  }
  function popBasket(b, g) {
    var team = root.querySelector(b.side === 'home' ? '.lv-home' : '.lv-team:not(.lv-home)');
    if (!team) return;
    var el = document.createElement('span');
    el.className = 'lv-pop lv-pop-' + b.pts;
    el.style.setProperty('--tc', b.side === 'home' ? g.hc : g.ac);
    el.textContent = '+' + b.pts;
    team.appendChild(el);
    var sc = team.querySelector('.lv-score'); if (sc) { sc.classList.remove('lv-bump'); void sc.offsetWidth; sc.classList.add('lv-bump'); }
    setTimeout(function () { el.remove(); }, 1800);
  }
  function applyFilter(mode) {
    root.querySelectorAll('.lv-filter [data-lf]').forEach(function (b) { b.classList.toggle('active', b.dataset.lf === mode); });
    root.querySelectorAll('tr.lv-nostar').forEach(function (tr) { tr.style.display = mode === 'all' ? '' : 'none'; });
    var anyStar = root.querySelector('tr.lv-star');
    if (mode === 'star' && !anyStar) applyFilter('all');
  }

  function tick(force) {
    if (busy || (!force && (!visible() || document.hidden))) return;
    busy = true;
    getJSON(API + 'scoreboard?dates=' + D.date).then(function (j) { parseScoreboard(j); lastErr = null; })
      .catch(function (e) { lastErr = e.message || 'network'; })
      .then(function () {
        if (!sel) { var live = (D.games || []).filter(function (g) { return (sb[g.gid] || {}).state === 'in'; })[0]; sel = (live || (D.games || [])[0] || {}).gid; }
        renderChips();
        var s = sb[sel];
        if (s && s.state !== 'pre') return getJSON(API + 'summary?event=' + sel).then(render).catch(function (e) { lastErr = e.message || 'network'; render(null); });
        render(null);
      })
      .then(function () { busy = false; }, function () { busy = false; });
  }
  function start() { if (!timer) timer = setInterval(tick, REFRESH); tick(true); }

  chips.addEventListener('click', function (e) { var b = e.target.closest('[data-lg]'); if (!b) return; sel = b.dataset.lg; history.replaceState(null, '', '#live-' + sel); renderChips(); root.innerHTML = '<div class="chart-empty">Loading…</div>'; tick(true); });
  root.addEventListener('click', function (e) { var b = e.target.closest('[data-lf]'); if (b) applyFilter(b.dataset.lf); });
  function fromHash() {
    var m = /^#live(?:-(\d+))?$/.exec(location.hash);
    if (!m) return false;
    if (m[1]) sel = m[1];
    if (typeof showTab === 'function') showTab('gview', 'live');
    start(); return true;
  }
  window.addEventListener('hashchange', fromHash);
  document.addEventListener('click', function (e) {
    var b = e.target.closest('[data-tabs="gview"] [data-tab="live"]'); if (b) start();
  });
  document.addEventListener('visibilitychange', function () { if (!document.hidden && visible()) tick(true); });
  fromHash();
})();
