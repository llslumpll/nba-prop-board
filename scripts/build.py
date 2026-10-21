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
import kalshi_client  # noqa: E402
import live  # noqa: E402
import logs  # noqa: E402
import markets  # noqa: E402
import model  # noqa: E402
import oddsapi_client  # noqa: E402
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
    """Once-a-day FanDuel pull that is safe to attempt several times a day
    (GitHub runs scheduled jobs late or not at all, so the workflow tries at
    several times). Game lines: pulled once per ET day. Points props: pulled
    once per ET day *with lines found*; an attempt where FanDuel hasn't posted
    yet costs nothing (empty responses aren't charged) and a later attempt
    tries again."""
    gl, pr = _load_json(LINES_CACHE, {}), _load_json(PROPS_CACHE, {})
    if _pulled_today_et(pr, need_lines=True):
        print("FanDuel: game lines and points props already pulled today; nothing to do (0 credits).")
        return {}
    if not _pulled_today_et(gl):
        st = pull_oddsapi(lines=True, props=True)
        return st
    print("FanDuel: game lines already pulled today; trying points props only.")
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
    res = live.run(box, sl, roster, inj, line_book, fit, spreads=spreads)
    print(f"Slate {sl.get('date')}: {len(sl['games'])} games, {len(res['players'])} players projected, "
          f"{sum(len(v) for v in res['picks'].values())} player/stat lines matched.")

    # ---- freeze (game-day window), re-freeze on late scratches, track closing lines
    preds_all = predictions.load_all()
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
                                     "tier", "proj_min", "var", "line", "chance", "break_even", "market_side")}
            rec.update(projected=p["proj"], call=p["side"], line_source=p["source"],
                       blend_mean=p.get("blend_mean"), blend_w=p.get("blend_w"),
                       lines={src: ln["line"] for src, ln in p["lines"].items()},
                       label=est[s]["status"], why=p["why"][:400], out_ctx=out_ctx)
            if predictions.freeze(rec, p["tip_utc"]):
                frozen += 1
            else:
                refrozen += predictions.refreeze(rec, p["tip_utc"])
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
    for g in sl["games"]:
        g["game_picks_open"] = live.game_picks_open(g, played)
        g["games_played"] = min(played.get(g["away"], 0), played.get(g["home"], 0))
        tp = res["teams"].get(g["game_id"])
        if not tp or not g["game_picks_open"]:
            continue
        base = {"game_id": g["game_id"], "date": g["date"], "home": g["home"], "away": g["away"],
                "our_margin": tp["margin"], "our_total": tp["total"]}
        hw = tp["home_win"]
        if (g["fanduel"] or {}).get("moneyline") or g.get("kalshi_ml"):
            pick = g["home"] if hw >= 0.5 else g["away"]
            tfrozen += predictions.freeze_team({**base, "market": "moneyline", "pick": pick,
                                                "chance": round(max(hw, 1 - hw), 4),
                                                "source": "fanduel" if (g["fanduel"] or {}).get("moneyline") else "kalshi"},
                                               g["tip_utc"])
        sp = (g["fanduel"] or {}).get("spread")
        if sp:
            c = live.team_market_chances(tp, sp["home_point"], None)["spread"]["p_home"]
            tfrozen += predictions.freeze_team({**base, "market": "spread", "line": sp["home_point"],
                                                "pick": g["home"] if c >= 0.5 else g["away"],
                                                "chance": round(max(c, 1 - c), 4), "source": "fanduel"}, g["tip_utc"])
        tot = (g["fanduel"] or {}).get("total")
        if tot:
            c = live.team_market_chances(tp, None, tot["point"])["total"]["p_over"]
            tfrozen += predictions.freeze_team({**base, "market": "total", "line": tot["point"],
                                                "pick": "OVER" if c >= 0.5 else "UNDER",
                                                "chance": round(max(c, 1 - c), 4), "source": "fanduel"}, g["tip_utc"])
    predictions.save()
    print(f"Froze {frozen} new player pick(s), re-froze {refrozen} (injury news or method update), updated {closes} closing line(s), "
          f"{tfrozen} new game pick(s).")

    # ---- Best 5 / Best Value (only frozen, untipped picks can enter; tipped ones stay locked)
    best = {}
    if sl.get("date"):
        groups = {s: [s] for s in ("pts", "reb", "ast", "fg3m")} | {"combos": ["pra", "pr", "pa", "ra"]}
        for grp, stats in groups.items():
            cands = [p for s in stats for p in res["picks"][s] if not p["started"] and not p.get("inj_status") and not p.get("role_gap")]
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

    # ---- grade, calibrate, score
    gr = grade.grade_all(box, sched)
    print(f"Grading: {gr}")
    preds_all = predictions.load_all()
    cal = grade.calibrate(preds_all)
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
        "season": dataio.season_status(now), "fit": fit, "box": box, "injuries": inj,
    })
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
