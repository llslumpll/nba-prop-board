"""
Static site builder for PROP EDGE NBA. Writes docs/*.html for GitHub Pages.

  python scripts/build.py              heavy build: refresh box scores, schedule,
                                       rosters, player variance; then everything below
  python scripts/build.py --no-fetch   light build: injuries + Kalshi + PrizePicks,
                                       re-project, freeze, grade, render
  --pull-lines   also pull FanDuel game lines from The Odds API (3 credits)
  --pull-props   also pull FanDuel points props for games tipping within 14h
                 (1 credit per game; only runs if the lines pull succeeded)
  --fanduel-daily  the scheduled FanDuel pull: lines once per ET day, props once
                 per ET day (retried until FanDuel posts them); safe to repeat
  --probe        fetch Kalshi + PrizePicks, print what came back, write
                 data/probe.json, render nothing (manual workflow)

Every source fails soft: an error is shown on the page and logged; the site
always builds; no number is ever invented.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import polars as pl  # noqa: E402

import dataio  # noqa: E402
import grade  # noqa: E402
import injuries_client  # noqa: E402
import lineups_client  # noqa: E402
import kalshi_client  # noqa: E402
import live  # noqa: E402
import logs  # noqa: E402
import markets  # noqa: E402
import model  # noqa: E402
import oddsapi_client  # noqa: E402
import espn_finals  # noqa: E402
import patterns  # noqa: E402
import rankings  # noqa: E402
import tune  # noqa: E402
import edges  # noqa: E402
import market  # noqa: E402
import parlays  # noqa: E402
import news  # noqa: E402
import pages  # noqa: E402
import predictions  # noqa: E402
import prizepicks_client  # noqa: E402
import rosters_client  # noqa: E402
import teams  # noqa: E402

ROOT = Path(__file__).parent.parent
DATA = ROOT / "data"
LINES_CACHE = DATA / "oddsapi_lines.json"
PROPS_CACHE = DATA / "oddsapi_props.json"


def _load_json(p, default=None):
    try:
        return json.loads(p.read_text())
    except Exception:  # noqa: BLE001
        return default


def _write_if_changed(p: Path, obj) -> None:
    txt = json.dumps(obj, indent=1, sort_keys=True, default=str)
    if not p.exists() or p.read_text() != txt:
        p.write_text(txt)


def pull_oddsapi(lines: bool, props: bool) -> dict:
    status = {}
    if not lines:
        return status
    print("The Odds API: pulling FanDuel game lines (3 credits)...")
    gl = oddsapi_client.fetch_game_lines()
    if gl["error"]:
        print(f"WARNING: game lines failed ({gl['error']}); keeping cache, skipping props.")
        status["lines_error"] = gl["error"]
        return status
    if gl["games"]:
        LINES_CACHE.write_text(json.dumps(gl, indent=1))
    else:
        print("FanDuel returned no NBA game lines; keeping cache.")
    print(f"FanDuel game lines: {gl['diagnostics']}")
    if props:
        rem = (gl["diagnostics"].get("usage") or {}).get("requests_remaining")
        pr = oddsapi_client.fetch_props(rem)
        print(f"FanDuel props diagnostics: {pr['diagnostics']}")
        if pr["error"]:
            status["props_error"] = pr["error"]
            print(f"WARNING: props failed ({pr['error']}); keeping cache.")
        elif not pr["lines"]:
            print("No FanDuel points props posted in the window yet; keeping cache.")
        else:
            PROPS_CACHE.write_text(json.dumps(pr, indent=1))
    u = oddsapi_client.current_usage()
    if u:
        print(f"The Odds API credits: {u['requests_used']} used, {u['requests_remaining']} remaining.")
    return status


def _pulled_today_et(cache: dict | None, need_lines: bool = False) -> bool:
    """True when this cache was pulled on today's US-Eastern date (and, for
    props, actually holds lines)."""
    from zoneinfo import ZoneInfo
    try:
        t = datetime.strptime((cache or {}).get("pulled_at", ""), "%Y-%m-%d %H:%M UTC").replace(tzinfo=timezone.utc)
    except ValueError:
        return False
    et = ZoneInfo("America/New_York")
    if t.astimezone(et).date() != datetime.now(timezone.utc).astimezone(et).date():
        return False
    return bool((cache or {}).get("lines")) if need_lines else True


def pull_fanduel_daily() -> dict:
    """FanDuel pulls, safe to attempt many times a day (GitHub runs scheduled
    jobs late or not at all, so several triggers fire).

    Paid plan (ODDS_PLAN set): every attempt pulls game lines + props
    (points, rebounds, assists, threes) for games tipping within 14 h.

    Free plan: game lines twice per ET day, once in the morning and once more
    after 3 PM ET (closer to tip, 3 credits); points props once per ET day
    with lines found (an attempt before FanDuel posts costs nothing and a
    later attempt retries)."""
    if oddsapi_client.PAID:
        print(f"FanDuel: paid plan ({oddsapi_client.PLAN}); pulling game lines and props.")
        return pull_oddsapi(lines=True, props=True)
    from zoneinfo import ZoneInfo
    et = ZoneInfo("America/New_York")
    gl, pr = _load_json(LINES_CACHE, {}), _load_json(PROPS_CACHE, {})
    now_et = datetime.now(timezone.utc).astimezone(et)
    try:
        gl_at = datetime.strptime(gl.get("pulled_at", ""), "%Y-%m-%d %H:%M UTC").replace(tzinfo=timezone.utc).astimezone(et)
    except ValueError:
        gl_at = None
    lines_today = _pulled_today_et(gl)
    late_due = now_et.hour >= 15 and lines_today and gl_at is not None and gl_at.hour < 15
    need_lines = (not lines_today) or late_due
    need_props = not _pulled_today_et(pr, need_lines=True)
    if not need_lines and not need_props:
        print("FanDuel: morning and afternoon game lines and today's points props already pulled; nothing to do (0 credits).")
        return {}
    if need_lines:
        print("FanDuel: " + ("afternoon game-lines refresh." if late_due else "first pull of the day."))
        return pull_oddsapi(lines=True, props=need_props)
    print("FanDuel: game lines current; trying points props only.")
    status = {}
    rem = (oddsapi_client.current_usage() or {}).get("requests_remaining")
    p = oddsapi_client.fetch_props(rem)
    print(f"FanDuel props diagnostics: {p['diagnostics']}")
    if p["error"]:
        status["props_error"] = p["error"]
    elif p["lines"]:
        PROPS_CACHE.write_text(json.dumps(p, indent=1))
    else:
        print("No FanDuel points props posted in the window yet; a later attempt will retry.")
    return status


def build(args) -> None:
    now = datetime.now(timezone.utc)
    DATA.mkdir(exist_ok=True)

    if not args.no_fetch:
        print("Refreshing sportsdataverse box scores + schedule...")
        dataio.refresh()
        print("Fetching ESPN rosters...")
        r = rosters_client.fetch()
        print(f"  rosters: {len(r.get('players', []))} players, error={r.get('error')}")
    box = dataio.load_box()
    sched = dataio.load_schedule()
    # same-night results: ESPN finals graded within minutes instead of next morning
    espn = espn_finals.refresh(sched, box, {p["date"] for p in predictions.load_all() + predictions.load_all(team=True)
                                             if p.get("status") == "pending"}, now)
    box, sched = espn_finals.merge_box(box, espn), espn_finals.merge_schedule(sched, espn)
    dataio._box_cache, dataio._sched_cache = box, sched  # every module now sees the merged tables
    print(f"ESPN results: {len(espn.get('status', {}))} games checked, {espn.get('new_finals', 0)} new final box score(s), "
          f"{len(espn.get('games', {}))} held until the daily file catches up, error={espn.get('error')}")
    fit = live.load_fit()
    if not args.no_fetch and box.height:
        n = live.refresh_player_var(box, fit)
        print(f"Player variance multipliers refreshed for {n} players.")

    print("Fetching ESPN injury report...")
    inj = injuries_client.fetch()
    print(f"  injuries: {len(inj.get('players', []))} listed, error={inj.get('error')}")
    rosters = rosters_client.load()

    print("Fetching Kalshi...")
    kal = kalshi_client.fetch_nba()
    print(f"  Kalshi per series: {kal['diagnostics']['per_series']}")
    pp = prizepicks_client.fetch_nba()
    print(f"  PrizePicks: error={pp['error']} diagnostics={pp['diagnostics']}")
    if args.probe:
        rosters = rosters_client.fetch()
        (DATA / "probe.json").write_text(json.dumps({"at": now.isoformat(), "kalshi": kal["diagnostics"],
                                                      "kalshi_error": kal["error"], "prizepicks": pp["diagnostics"],
                                                      "prizepicks_error": pp["error"],
                                                      "injuries_error": inj.get("error"),
                                                      "rosters_error": rosters.get("error"),
                                                      "rosters_players": len(rosters.get("players", [])),
                                                      "injuries_listed": len(inj.get("players", []))}, indent=1))
        print("Probe written to data/probe.json")
        return
    _write_if_changed(DATA / "kalshi_raw.json", {"error": kal["error"], "diagnostics": kal["diagnostics"],
                                                  "game": kal["game"][:400]})

    odds_status = pull_fanduel_daily() if args.fanduel_daily else pull_oddsapi(args.pull_lines, args.pull_props)
    gl_cache = _load_json(LINES_CACHE, {}) or {}
    pr_cache = _load_json(PROPS_CACHE, {}) or {}
    fd_games = oddsapi_client.upcoming_only(gl_cache.get("games", []))
    fd_props = oddsapi_client.upcoming_only(pr_cache.get("lines", []))

    # ---- slate, lines, projections
    sl = live.slate(now)
    roster, roster_src = live.roster_rows(box, rosters)
    roster_team = {name: team for _pid, name, team in roster}
    # final check before tip: NBA.com starting lineups + official inactives
    lineups = {}
    if sl.get("games"):
        try:
            ros = [{"player": n, "team": t} for _pid, n, t in roster]
            lineups = lineups_client.fetch(sl["date"], ros) if not args.no_fetch else lineups_client.load()
            if lineups.get("date") == sl["date"]:
                inj, n_out = lineups_client.merge(inj, lineups)
                conf = sum(1 for t in (lineups.get("teams") or {}).values() if t.get("status") == "Confirmed")
                print(f"Lineups: {lineups.get('games', 0)} games, {conf} teams confirmed, {n_out} newly ruled out from official inactives, "
                      f"{len(lineups.get('unmatched') or [])} names unmatched, error={lineups.get('error')}")
            else:
                print(f"Lineups: none for {sl['date']} yet, error={lineups.get('error')}")
        except Exception as e:  # noqa: BLE001
            print(f"Lineups failed: {e}")
    known = set(roster_team.items())
    if box.height:
        known |= set(box.filter(pl.col("season") >= dataio.CURRENT_SEASON - 1)
                     .select("player", "team").unique().iter_rows())
    matcher = markets.NameMatcher(known)
    line_book = markets.build_lines(sl["games"], matcher, roster_team, fd_props, pp, kal)
    spreads = {}
    for g in sl["games"]:
        go = oddsapi_client.game_odds(fd_games, g["away"], g["home"]) or {}
        g["fanduel"] = go
        g["kalshi_ml"] = kalshi_client.moneyline(kal, g["away"], g["home"], g["date"])
        if go.get("spread"):
            spreads[g["game_id"]] = go["spread"]["home_point"]
    # unit sizing trust, from how sized picks have actually done (see live.unit_rec)
    trust = grade.unit_trust(predictions.load_all(), predictions.load_all(team=True))
    live.UNIT_TRUST = trust["trust"]
    print(f"Unit sizing trust {trust['trust']}: {trust['why']}")
    # pattern corrections that have held up on later games (computed by the previous build, see patterns.py)
    pat = patterns.active()
    live.GAME_SKIP = {k for k in pat["skip"] if k.startswith("game|")}
    live.GAME_FLIP = {k: v for k, v in pat["flip"].items() if k.startswith("game|")}
    print(f"Pattern corrections active: {len(pat['skip'])} skip, {len(pat['over'])} over-bias, {len(pat['move'])} line-move, "
          f"confidence {pat['conf']}, flip {pat['flip']}")
    open_lines = {}
    if sl.get("date"):
        for p in predictions.load_date(sl["date"]).values():
            op = (p.get("snaps") or {}).get("open")
            if op and op.get("line") is not None:
                open_lines[(p["player"], p["stat"], p["game_id"])] = (op.get("line_source"), op["line"])
    # tonight's own results never feed tonight's projections
    sl_ids = [g["game_id"] for g in sl["games"]]
    try:  # "picks like this hit H/N": every graded pick's group record, for proven units and the cards
        import proven
        live.PROVEN = proven.compute(predictions.load_all(), predictions.load_all(team=True))
        npv = sum(1 for k, r in live.PROVEN.items() if r["n"] >= proven.MIN_N and r["h"] / r["n"] >= 0.8)
        print(f"Proven groups: {len(live.PROVEN)} tracked, {npv} hitting 80%+ over {proven.MIN_N}+ picks")
    except Exception as e:  # noqa: BLE001
        print(f"Proven groups failed: {e}")
    box_pre = box.filter(~pl.col("game_id").is_in(sl_ids)) if box.height and sl_ids else box
    try:  # injury news found by earlier builds today moves teammates' projections (news.projection_adj)
        news_adj = news.projection_adj(sl.get("date"), box_pre)
    except Exception as e:  # noqa: BLE001
        news_adj = {}
        print(f"News projection adjustments failed: {e}")
    res = live.run(box_pre, sl, roster, inj, line_book, fit, spreads=spreads, pat=pat, open_lines=open_lines, news_adj=news_adj)
    # injury news: who was newly ruled out since the last build. New news re-projects right away, so this
    # build's picks, lists and parlays already use it (not 20 minutes later)
    try:
        nw = news.process(sl, inj, res, now, box=box_pre)
        print(f"Injury news: {nw}")
        if nw.get("new"):
            news_adj = news.projection_adj(sl.get("date"), box_pre)
            res = live.run(box_pre, sl, roster, inj, line_book, fit, spreads=spreads, pat=pat, open_lines=open_lines, news_adj=news_adj)
            print(f"Re-projected after the news: {len(news_adj)} teammates adjusted")
    except Exception as e:  # noqa: BLE001
        print(f"Injury news failed: {e}")
    print(f"Slate {sl.get('date')}: {len(sl['games'])} games, {len(res['players'])} players projected, "
          f"{sum(len(v) for v in res['picks'].values())} player/stat lines matched.")
    # projection snapshot for the audit's sanity checks (minutes, lines, injuries, duplicates)
    try:
        (DATA / "projections_today.json").write_text(json.dumps({
            "at": now.strftime("%Y-%m-%d %H:%M UTC"), "date": sl.get("date"),
            "games": {str(g["game_id"]): {"tip": str(g["tip_utc"]), "started": bool(g.get("started")), "teams": [g["away"], g["home"]]} for g in sl["games"]},
            "players": [{"player": r["player"], "id": r["player_id"], "team": r["team"], "game_id": r["game_id"],
                         "proj_min": r["proj_min"], "recent_min": (r.get("factors") or {}).get("min_recent"),
                         "games": (r.get("factors") or {}).get("games_this_season"), "floor": (r.get("factors") or {}).get("min_floor"),
                         "inj": r.get("inj_status"), "has_line": any((st or {}).get("lines") for st in (r.get("stats") or {}).values())}
                        for r in res["players"]]}, default=str))
    except Exception as e:  # noqa: BLE001
        print(f"Projection snapshot failed: {e}")


    # ---- freeze (game-day window), re-freeze on late scratches, track closing lines
    preds_all = predictions.load_all()
    # weekly self-tuning: re-test the model's settings on played games; keep only changes that hold up
    try:
        if tune.due(now):
            tr = tune.run(box, fit.get("params_base") or fit["params"], now)
            if tr.get("ran"):
                ch = "; ".join(f"{c['knob']} {c['from']} -> {c['to']}" for c in tr["changes"]) or "no change (current settings are the best nearby)"
                print(f"Self-tuning: tried {tr['tried']} settings; {ch}; average miss {tr['gain']}")
            else:
                print(f"Self-tuning skipped: {tr.get('why')}")
    except Exception as e:  # noqa: BLE001
        print(f"Self-tuning failed: {e}")
    est = grade.edge_status(preds_all)
    outs = {}
    for ip in inj.get("players", []):
        if ip.get("class") == "out" and ip.get("team"):
            outs.setdefault(ip["team"], set()).add(ip.get("player"))
    frozen = refrozen = closes = 0
    for s, plist in res["picks"].items():
        for p in plist:
            out_ctx = sorted(outs.get(p["team"], set()) | outs.get(p["opp"], set()))
            rec = {k: p[k] for k in ("player", "player_id", "team", "opp", "home", "game_id", "date", "stat",
                                     "tier", "proj_min", "var", "line", "chance", "break_even", "market_side", "units", "units_raw", "role_gap",
                                     "skip", "move", "move_dir", "adj", "flip", "be_over", "be_under", "src_key", "anchor")}
            rec.update(projected=p["proj"], call=p["side"], line_source=p["source"],
                       blend_mean=p.get("blend_mean"), blend_w=p.get("blend_w"),
                       lines={src: ln["line"] for src, ln in p["lines"].items()},
                       mkt={src: [ln["line"], ln.get("be_over"), ln.get("be_under")] for src, ln in p["lines"].items()},
                       kladder=market.ladder_snapshot((p["lines"] or {}).get("kalshi")),
                       label=est[s]["status"], why=p["why"][:400], out_ctx=out_ctx)
            if predictions.freeze(rec, p["tip_utc"]):
                frozen += 1
            else:
                refrozen += predictions.update(rec, p["tip_utc"])  # keeps following lines and news until tip
            cur = predictions.get(p["date"], p["player"], s, p["game_id"])
            if cur:
                ln = (p["lines"] or {}).get(cur.get("line_source"))
                if ln:
                    mo = ln.get("market_over")
                    mkt_side = (mo if cur["call"] == "OVER" else 1 - mo) if mo is not None else None
                    closes += predictions.record_close(p["date"], p["player"], s, p["game_id"], ln["line"],
                                                       round(mkt_side, 4) if mkt_side is not None else None, p["tip_utc"])
    tfrozen = 0
    played = live.team_games_played(box)
    game_ranked, market_ranked = [], {}
    gaps = [res["teams"][g["game_id"]]["total"] - float(g["fanduel"]["total"]["point"]) for g in sl["games"]
            if res["teams"].get(g["game_id"]) and (g.get("fanduel") or {}).get("total")]
    total_offset = sum(gaps) / len(gaps) if len(gaps) >= 3 else 0.0
    # how well our team numbers track the market, pooled over every game so far (this slate + earlier frozen picks)
    seen_m, seen_t = {}, {}
    for t in predictions.load_all(team=True):
        if t.get("our_margin") is not None and t.get("market_margin") is not None and t["market"] in ("spread", "moneyline"):
            seen_m[t["game_id"]] = (t["market_margin"], t["our_margin"])
        if t["market"] == "total" and t.get("our_total") is not None and t.get("line") is not None:
            seen_t[t["game_id"]] = (t["line"], t["our_total"])
    for g in sl["games"]:
        tp0, fd0 = res["teams"].get(g["game_id"]), g.get("fanduel") or {}
        if tp0 and fd0.get("spread"):
            seen_m[g["game_id"]] = (-float(fd0["spread"]["home_point"]), tp0["margin"])
        if tp0 and fd0.get("total"):
            seen_t[g["game_id"]] = (float(fd0["total"]["point"]), tp0["total"] - total_offset)
    agree_m, agree_t = live.agreement(list(seen_m.values())), live.agreement(list(seen_t.values()))
    print(f"Game picks: our margins vs market r={agree_m}, totals r={agree_t} over {len(seen_m)}/{len(seen_t)} games")
    for g in sl["games"]:
        g["games_played"] = {t: played.get(t, 0) for t in (g["away"], g["home"])}
        tp = res["teams"].get(g["game_id"])
        g["game_picks"] = live.game_picks(g, tp, g.get("kalshi_ml"), total_offset, agree_m, agree_t) if tp else []
        base = {"game_id": g["game_id"], "date": g["date"], "home": g["home"], "away": g["away"],
                "our_margin": tp["margin"] if tp else None, "our_total": tp["total"] if tp else None}
        fd = g.get("fanduel") or {}
        for gp in g["game_picks"]:
            if predictions.freeze_team({**base, **gp}, g["tip_utc"]):
                tfrozen += 1
            else:
                predictions.update_team({**base, **gp}, g["tip_utc"])
            cur = predictions.load_date(g["date"], team=True).get(f"{g['game_id']}|{gp['market']}")
            if cur:
                if gp["market"] == "spread" and fd.get("spread"):
                    predictions.record_close_team(g["date"], g["game_id"], "spread", fd["spread"]["home_point"], None, g["tip_utc"])
                elif gp["market"] == "total" and fd.get("total"):
                    predictions.record_close_team(g["date"], g["game_id"], "total", fd["total"]["point"], None, g["tip_utc"])
                elif gp["market"] == "moneyline":
                    mlp = (fd.get("moneyline") or {})
                    hp_ = mlp.get("home_prob") if mlp else (g.get("kalshi_ml") or {}).get("home_prob")
                    if hp_ is not None:
                        side = hp_ if cur["pick"] == g["home"] else 100 - hp_
                        predictions.record_close_team(g["date"], g["game_id"], "moneyline", None, round(side / 100, 4), g["tip_utc"])
            if not g["started"] and not gp.get("skip"):
                market_ranked.setdefault(gp["market"], []).append({**gp, "game_id": g["game_id"]})
            if not g["started"] and not gp.get("skip") and (gp.get("value") is None or gp["value"] >= 0):  # only picks that beat their price
                game_ranked.append({**gp, "game_id": g["game_id"]})
    if sl.get("date"):
        predictions.sync_team_best(sl["date"], "best5", sorted(game_ranked, key=lambda r: -r["chance"]))
        predictions.sync_team_best(sl["date"], "value", sorted([r for r in game_ranked if r.get("value") is not None],
                                                              key=lambda r: -r["value"]))
        # per market (Games page tabs): Best 5 = highest chance, like the player lists; Best Value = beats its price, biggest edge
        for mkt in ("moneyline", "spread", "total"):
            rk = market_ranked.get(mkt, [])
            predictions.sync_team_best(sl["date"], "best5", sorted(rk, key=lambda r: -r["chance"]), tag=f"{mkt}:best5")
            predictions.sync_team_best(sl["date"], "value", sorted([r for r in rk if r.get("value") is not None and r["value"] >= 0],
                                                                  key=lambda r: -r["value"]), tag=f"{mkt}:value")
    predictions.save()
    print(f"Recorded {frozen} new player pick(s), updated {refrozen} (line, side or chance moved before tip), {closes} closing line(s), "
          f"{tfrozen} new game pick(s).")

    # PARLAYS: the site's own 2-4 leg parlays, rebuilt until the first leg tips, then locked and graded
    try:
        if sl.get("date"):
            pr = parlays.sync(sl["date"], now)
            print(f"Parlays: {pr}")
    except Exception as e:  # noqa: BLE001
        print(f"Parlays failed: {e}")

    # EDGES: Kalshi prices vs FanDuel's fair price (market-based, no projection), recorded and graded
    try:
        edge_found = edges.find(sl, kal, line_book, res)
        n_edge = edges.record(edge_found, now)
        try:  # Kalshi lagging the books (market.py): recorded with the real price, tracked until proven
            lags = market.find_lags(line_book, sl)
            print(f"Kalshi lags: {len(lags)} open, {market.record(lags, now)} new")
        except Exception as e:  # noqa: BLE001
            print(f"Kalshi lags failed: {e}")
        print(f"EDGES: {len(edge_found['edges'])} +EV of {edge_found['checked']} matched Kalshi prices, "
              f"{len(edge_found['arbs'])} arbitrage, {len(edge_found['middles'])} middles; {n_edge} newly recorded")
    except Exception as e:  # noqa: BLE001  (never let the extra page stop a build)
        edge_found = {"edges": [], "arbs": [], "middles": [], "closest_arb": None, "checked": 0, "error": str(e)[:200]}
        print(f"EDGES failed: {e}")

    if predictions.apply_fixes():
        predictions.save()
    # ---- Best 5 / Best Value (only frozen, untipped picks can enter; tipped ones stay locked)
    best = {}
    if sl.get("date"):
        groups = {s: [s] for s in ("pts", "reb", "ast", "fg3m")} | {"combos": ["pra", "pr", "pa", "ra"]}
        for grp, stats in groups.items():
            cands = [p for s in stats for p in res["picks"][s] if not p["started"] and not p.get("inj_status") and not p.get("role_gap")
                     and not p.get("skip")]  # pattern-skipped groups stay off the lists
            conf = sorted(cands, key=lambda p: -p["chance"])
            val = sorted([p for p in cands if p.get("value") is not None], key=lambda p: -p["value"])
            for name, ranked in (("best5", conf), ("value", val)):
                predictions.sync_best5(sl["date"], grp, name, ranked)
        predictions.save()
        store = predictions.load_date(sl["date"])
        for grp in groups:
            best[grp] = {}
            for name, sk in (("best5", "chance"), ("value", "value")):
                tagged = [dict(p) for p in store.values() if f"{grp}:{name}" in p.get("best5", [])]
                for p in tagged:
                    p["value"] = round(p["chance"] - p["break_even"], 4) if p.get("break_even") is not None else None
                tagged.sort(key=lambda p: -(p.get(sk) or 0))
                best[grp][name] = tagged
        n_snap = predictions.snapshot(sl["date"])  # 10 AM / 1 PM / 5 PM CT copies, Best 5 tags included
        if n_snap:
            print(f"Checkpoint snapshot: saved {n_snap} pick(s).")
        predictions.save()

    # ---- grade, calibrate, score
    gr = grade.grade_all(box, sched)
    try:
        gr["edges"] = edges.grade(box, sched)
    except Exception as e:  # noqa: BLE001
        print(f"EDGES grading failed: {e}")
    try:
        gr["market"] = market.grade(now)
    except Exception as e:  # noqa: BLE001
        print(f"Kalshi lag grading failed: {e}")
    try:
        gr["news"] = news.grade()
    except Exception as e:  # noqa: BLE001
        print(f"News grading failed: {e}")
    try:
        gr["parlays"] = parlays.grade(now)["newly_graded"]
    except Exception as e:  # noqa: BLE001
        print(f"Parlay grading failed: {e}")
    print(f"Grading: {gr}")
    preds_all = predictions.load_all()
    cal = grade.calibrate(preds_all)
    pat_out = patterns.compute(preds_all, predictions.load_all(team=True), now)
    print("Patterns: " + ", ".join(f"{k} {sum(1 for r in pat_out['rules'] if r['status'] == k)}" for k in ("active", "watching", "flipped", "fine")))
    est = grade.edge_status(preds_all)

    lm = logs.log_lines(line_book, sl.get("date"))
    teams_unmatched = teams.unmatched_report()
    _write_if_changed(DATA / "unmatched_names.json", {"teams": teams_unmatched, "players": matcher.unmatched})
    src_counts = {}
    for srcs in line_book.values():
        for s in srcs:
            src_counts[s] = src_counts.get(s, 0) + 1
    health = logs.log_health({
        "mode": "heavy" if not args.no_fetch else "light",
        "slate": sl.get("date"), "games": len(sl["games"]), "players": len(res["players"]),
        "lines": src_counts, "frozen": frozen, "refrozen": refrozen, "team_frozen": tfrozen, "graded": gr["newly_graded"],
        "errors": {k: v for k, v in {"kalshi": kal["error"], "prizepicks": pp["error"], "injuries": inj.get("error"),
                                     "rosters": rosters.get("error"), **odds_status}.items() if v},
    })

    sources = {
        "box": dataio.source_status(), "injuries": {"at": inj.get("fetched_at"), "error": inj.get("error"),
                                                    "n": len(inj.get("players", []))},
        "rosters": {"at": rosters.get("fetched_at"), "error": rosters.get("error"),
                    "n": len(rosters.get("players", [])), "used": roster_src},
        "kalshi": {"error": kal["error"], "per_series": kal["diagnostics"]["per_series"]},
        "prizepicks": {"error": pp["error"], "n": len(pp["lines"])},
        "fanduel": {"lines_at": gl_cache.get("pulled_at"), "props_at": pr_cache.get("pulled_at"),
                    "games": len(fd_games), "props": len(fd_props), **odds_status,
                    "usage": oddsapi_client.current_usage()},
        "line_counts": src_counts,
    }
    pages.render_all({
        "now": now, "slate": sl, "res": res, "best": best, "edge": est, "cal": cal, "preds": preds_all,
        "tpreds": predictions.load_all(team=True), "sources": sources, "health": health, "lm": lm,
        "season": dataio.season_status(now), "fit": fit, "box": box, "injuries": inj, "trust": trust, "espn": espn, "patterns": pat_out, "rankings": rankings.build(box), "edges": edge_found, "edges_sum": edges.summary(), "tuning": tune.load(), "parlays": parlays.summary(), "news": news.summary(sl.get("date")), "lag": market.lag_summary(sl.get("date")), "lineups": lineups,
    })
    # consistency audit on what was just built; its result shows on History -> Pipeline next build
    import subprocess
    r = subprocess.run([sys.executable, str(Path(__file__).parent / "audit.py")], capture_output=True, text=True)
    lines = [ln for ln in r.stdout.splitlines() if ln.startswith(("PASS", "FAIL"))]
    _write_if_changed(DATA / "audit.json", {"at": now.strftime("%Y-%m-%d %H:%M UTC"), "passed": r.returncode == 0,
                                            "checks": len(lines), "fails": [ln[5:] for ln in lines if ln.startswith("FAIL")]})
    print("Audit:", "all checks passed" if r.returncode == 0 else r.stdout[-800:])
    print(f"Build complete at {now:%Y-%m-%d %H:%M UTC}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-fetch", action="store_true")
    ap.add_argument("--pull-lines", action="store_true")
    ap.add_argument("--pull-props", action="store_true")
    ap.add_argument("--probe", action="store_true")
    ap.add_argument("--fanduel-daily", action="store_true",
                    help="pull FanDuel lines/props at most once per ET day; safe to run several times")
    build(ap.parse_args())
