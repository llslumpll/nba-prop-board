/* Prop Edge NBA slip builder. Runs entirely in the browser; the slip is kept
   in this browser only (localStorage) and is never sent anywhere.
   Chances come from the site; payouts default to PrizePicks' standard
   multipliers and can be edited, since PrizePicks changes them. */
(function () {
  var KEY = 'pe-slip-v1', PKEY = 'pe-pay-v1';
  var DEF_POWER = {2: 3, 3: 5, 4: 10, 5: 20, 6: 37.5};
  var DEF_FLEX = {3: {3: 3, 2: 1}, 4: {4: 6, 3: 1.5}, 5: {5: 10, 4: 2, 3: 0.4}, 6: {6: 25, 5: 2, 4: 0.4}};
  function load(k, d) { try { var v = JSON.parse(localStorage.getItem(k)); return v || d; } catch (e) { return d; } }
  function save(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch (e) {} }
  var state = load(KEY, {legs: [], type: 'power', open: false});
  var pay = load(PKEY, {power: DEF_POWER, flex: DEF_FLEX});
  var el = document.getElementById('slip');
  if (!el) return;

  function has(id) { return state.legs.some(function (l) { return l.id === id; }); }
  function add(leg) { if (!has(leg.id) && state.legs.length < 6) state.legs.push(leg); }
  function remove(id) { state.legs = state.legs.filter(function (l) { return l.id !== id; }); }
  function pct(x) { return (x * 100).toFixed(1) + '%'; }
  function esc(s) { return String(s).replace(/[&<>"']/g, function (c) { return {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]; }); }

  // chance of exactly k hits (legs treated as independent)
  function dist(ps) {
    var d = [1];
    ps.forEach(function (p) {
      var n = new Array(d.length + 1).fill(0);
      d.forEach(function (v, k) { n[k] += v * (1 - p); n[k + 1] += v * p; });
      d = n;
    });
    return d;
  }
  function warnings(legs) {
    var out = [], byGame = {}, byTeam = {}, byPlayer = {};
    legs.forEach(function (l) {
      (byGame[l.gid] = byGame[l.gid] || []).push(l);
      (byTeam[l.team] = byTeam[l.team] || []).push(l);
      (byPlayer[l.pid] = byPlayer[l.pid] || []).push(l);
    });
    Object.keys(byPlayer).forEach(function (k) { var a = byPlayer[k]; if (a.length > 1) out.push({lvl: 'bad', t: a[0].player + ' appears ' + a.length + ' times: these legs move together almost completely (and PrizePicks may not allow it).'}); });
    Object.keys(byTeam).forEach(function (k) { var a = byTeam[k]; var names = a.map(function (l) { return l.player; }).filter(function (v, i, s) { return s.indexOf(v) === i; }); if (names.length > 1) out.push({lvl: 'warn', t: names.join(', ') + ' are teammates (' + k + '): their results are linked, so the real chance is further from the number shown.'}); });
    Object.keys(byGame).forEach(function (k) { var a = byGame[k]; var tms = a.map(function (l) { return l.team; }).filter(function (v, i, s) { return s.indexOf(v) === i; }); if (tms.length > 1) out.push({lvl: 'warn', t: 'Same game: ' + a.map(function (l) { return l.player; }).join(', ') + '. Pace and blowouts affect every leg.'}); });
    if (legs.some(function (l) { return l.src !== 'prizepicks'; })) out.push({lvl: 'info', t: 'Some legs use a FanDuel or Kalshi line; PrizePicks may post a different number, which changes the chance.'});
    return out;
  }
  function render() {
    var legs = state.legs, n = legs.length;
    document.querySelectorAll('.slip-add').forEach(function (b) {
      var id = JSON.parse(b.dataset.leg).id; var on = has(id);
      b.classList.toggle('in-slip', on); b.textContent = on ? '✓ SLIP' : '+ SLIP';
    });
    el.classList.toggle('hidden', n === 0);
    document.body.classList.toggle('has-slip', n > 0);
    if (!n) { save(KEY, state); return; }
    var ps = legs.map(function (l) { return l.chance; });
    var d = dist(ps), type = state.type;
    if (type === 'flex' && n < 3) type = 'power';
    var table = type === 'power' ? (pay.power[n] ? (function () { var t = {}; t[n] = pay.power[n]; return t; })() : {}) : (pay.flex[n] || {});
    var ev = 0, rows = '';
    for (var k = n; k >= 0; k--) {
      var mult = table[k];
      if (mult === undefined && !(type === 'power' && k === n)) continue;
      ev += d[k] * (mult || 0);
      rows += '<tr><td>' + k + ' of ' + n + ' hit</td><td class="num">' + pct(d[k]) + '</td><td class="num"><input class="slip-mult" data-k="' + k + '" type="number" step="0.05" min="0" value="' + (mult === undefined ? '' : mult) + '">x</td></tr>';
    }
    var evPct = (ev - 1) * 100, winAll = d[n];
    var head = '<button class="slip-bar" type="button"><span class="slip-ic">🎟</span> SLIP ' + n +
      ' <span class="slip-mini">' + (type === 'power' ? 'POWER' : 'FLEX') + ' · all hit ' + pct(winAll) +
      ' · <b class="' + (evPct >= 0 ? 'ev-up' : 'ev-down') + '">' + (evPct >= 0 ? '+' : '') + evPct.toFixed(1) + '%</b></span><span class="slip-caret">' + (state.open ? '▼' : '▲') + '</span></button>';
    var body = '';
    if (state.open) {
      body += '<div class="slip-body"><div class="slip-legs">' + legs.map(function (l) {
        return '<div class="slip-leg"><div><b>' + esc(l.player) + '</b> <span class="dim">' + esc(l.team) + ' vs ' + esc(l.opp) + '</span><div><span class="pp-pick pp-' + l.side.toLowerCase() + '">' + l.side + ' ' + l.line + ' ' + esc(l.stat) + '</span> <span class="pp-ch">' + Math.round(l.chance * 100) + '%</span> <span class="src">' + esc(l.src) + '</span></div></div><button class="slip-x" type="button" data-rm="' + esc(l.id) + '" aria-label="Remove">✕</button></div>';
      }).join('') + '</div>';
      body += '<div class="slip-type"><button type="button" data-type="power" class="' + (type === 'power' ? 'active' : '') + '">POWER</button><button type="button" data-type="flex" class="' + (type === 'flex' ? 'active' : '') + '"' + (n < 3 ? ' disabled title="Flex needs 3+ picks"' : '') + '>FLEX</button></div>';
      body += '<table class="slip-table"><thead><tr><th>Result</th><th class="num">Chance</th><th class="num">Pays</th></tr></thead><tbody>' + rows + '</tbody></table>';
      body += '<div class="slip-sum"><div><em>ALL HIT</em><b>' + pct(winAll) + '</b></div><div><em>$10 RETURNS ON AVG</em><b class="' + (evPct >= 0 ? 'ev-up-t' : 'ev-down-t') + '">$' + (ev * 10).toFixed(2) + '</b></div><div><em>EXPECTED VALUE</em><b class="' + (evPct >= 0 ? 'ev-up-t' : 'ev-down-t') + '">' + (evPct >= 0 ? '+' : '') + evPct.toFixed(1) + '%</b></div></div>';
      var w = warnings(legs);
      if (w.length) body += '<div class="slip-warn">' + w.map(function (x) { return '<p class="sw-' + x.lvl + '">⚠ ' + esc(x.t) + '</p>'; }).join('') + '</div>';
      body += '<p class="slip-note">Chances are ours and treat legs as independent (see warnings), and they stay leans until History shows an edge. Payouts are PrizePicks’ standard multipliers; edit them to match your app. Saved in this browser only.</p>';
      body += '<div class="slip-btns"><button type="button" class="chip" data-reset-pay="1">RESET PAYOUTS</button><button type="button" class="chip" data-clear="1">CLEAR SLIP</button></div></div>';
    }
    el.innerHTML = head + body;
    save(KEY, state);
  }
  document.addEventListener('click', function (e) {
    var b = e.target.closest('button'); if (!b) return;
    if (b.classList.contains('slip-add')) { var leg = JSON.parse(b.dataset.leg); if (has(leg.id)) remove(leg.id); else add(leg); render(); return; }
    if (b.dataset.fill) {
      var legs = JSON.parse(b.dataset.fill), games = {};
      state.legs.forEach(function (l) { games[l.gid] = 1; });
      legs.forEach(function (l) { if (b.dataset.onePerGame) { if (games[l.gid]) return; games[l.gid] = 1; } add(l); });
      state.open = true; render(); return;
    }
    if (!el.contains(b)) return;
    if (b.classList.contains('slip-bar')) { state.open = !state.open; render(); }
    else if (b.dataset.rm) { remove(b.dataset.rm); render(); }
    else if (b.dataset.type && !b.disabled) { state.type = b.dataset.type; render(); }
    else if (b.dataset.clear) { state.legs = []; state.open = false; render(); }
    else if (b.dataset.resetPay) { pay = {power: DEF_POWER, flex: DEF_FLEX}; save(PKEY, pay); render(); }
  });
  el.addEventListener('change', function (e) {
    var i = e.target; if (!i.classList.contains('slip-mult')) return;
    var n = state.legs.length, k = +i.dataset.k, v = parseFloat(i.value);
    var type = (state.type === 'flex' && n >= 3) ? 'flex' : 'power';
    if (type === 'power') { pay.power = Object.assign({}, pay.power); pay.power[n] = isNaN(v) ? 0 : v; }
    else { pay.flex = JSON.parse(JSON.stringify(pay.flex)); pay.flex[n] = pay.flex[n] || {}; if (isNaN(v) || v === 0) delete pay.flex[n][k]; else pay.flex[n][k] = v; }
    save(PKEY, pay); render();
  });
  render();
})();
