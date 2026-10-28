"""
Renders docs/*.html from the build results. All counts and copy on the pages
are computed here from data, never hardcoded.

History is split: history.html is the summary; each month's pick-by-pick
list is its own page (history-YYYY-MM.html), so no page grows all season.
"""

from __future__ import annotations

import json
import shutil
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from jinja2 import Environment, FileSystemLoader

import grade
import history_view
import live
import logs
import markets
import model
import predictions
import rankings
import svgchart
import teams

ROOT = Path(__file__).parent.parent
DOCS = ROOT / "docs"
TEMPLATES = Path(__file__).parent / "templates"
ET = ZoneInfo("America/New_York")

NAV = [
    ("home", "Home", "index.html"),
    ("picks", "Picks", "picks.html"),
    ("games", "Games", "games.html"),
    ("players", "Players", "players.html"),
    ("rankings", "Teams", "rankings.html"),
    ("parlays", "Parlays", "parlays.html"),
    ("history", "History", "history.html"),
]
NAV_ICON = {"home": "\u2302", "picks": "\u2605", "games": "\u25ce", "players": "\u25a6", "rankings": "\u2630", "parlays": "\u2726", "history": "\u21ba"}
# Old URLs from the 9-page layout forward to their new home, so bookmarks keep working.
REDIRECTS = {"points.html": "picks.html#pts", "rebounds.html": "picks.html#reb", "assists.html": "picks.html#ast",
             "threes.html": "picks.html#fg3m", "pra.html": "picks.html#combos",
             "moneyline.html": "games.html", "matchups.html": "games.html"}
BOX_STATS = ["pts", "reb", "ast", "fg3m", "pra"]
ALL_STATS = ["pts", "reb", "ast", "fg3m", "pra", "pr", "pa", "ra"]

STAT_PAGES = [
    ("points", "Points", "points.html", ["pts"], "pts",
     "Projected points for every rotation player on the slate, against FanDuel, Kalshi and PrizePicks lines."),
    ("rebounds", "Rebounds", "rebounds.html", ["reb"], "reb",
     "Projected rebounds, with the chance of each side from a count distribution fit to each player's own spread of results."),
    ("assists", "Assists", "assists.html", ["ast"], "ast",
     "Projected assists. Assists move most when a team's main ball-handler is out; the minutes model handles that through the team's rotation."),
    ("threes", "3-Pointers", "threes.html", ["fg3m"], "fg3m",
     "Projected 3-pointers made. The noisiest of the main stats, so chances here sit closer to 50%."),
    ("pra", "PRA & Combos", "pra.html", ["pra", "pr", "pa", "ra"], "combos",
     "Points + rebounds + assists, and the three two-stat combos PrizePicks lists (pts+reb, pts+ast, reb+ast). Best 5 and Best Value take one combo per player."),
]
STAT_SHORT = {"pts": "PTS", "reb": "REB", "ast": "AST", "fg3m": "3PM", "pra": "PRA", "pr": "P+R", "pa": "P+A", "ra": "R+A"}
GROUP_PAGE = {"pts": "Points", "reb": "Rebounds", "ast": "Assists", "fg3m": "3-Pointers", "combos": "PRA & Combos"}
GROUP_HREF = {g: f"picks.html#{g}" for g in ("pts", "reb", "ast", "fg3m", "combos")}
GROUP_SHORT = {"pts": "PTS", "reb": "REB", "ast": "AST", "fg3m": "3PM", "combos": "PRA+"}


def _fmtline(x):
    if x is None:
        return "—"
    x = float(x)
    return f"{x:g}" if x.is_integer() else f"{x:.1f}"


def _fmtpt(x):
    if x is None:
        return "—"
    return f"+{float(x):g}" if x > 0 else f"{float(x):g}"


def _fmtnum(x):
    return _fmtline(x)


def _season_label(s):
    return f"{int(s) - 1}-{str(int(s))[-2:]}"


def _css_hash() -> str:
    import hashlib
    try:
        return hashlib.md5((DOCS / "static" / "style.css").read_bytes()).hexdigest()[:10]
    except Exception:  # noqa: BLE001
        return "0"


def _env():
    env = Environment(loader=FileSystemLoader(str(TEMPLATES)), autoescape=True, trim_blocks=True, lstrip_blocks=True)
    env.filters.update(fmtline=_fmtline, fmtpt=_fmtpt, fmtnum=_fmtnum, season_label=_season_label)
    env.globals.update(stat_labels=model.STAT_LABEL, stat_short=STAT_SHORT, src_labels=markets.SOURCE_LABEL)
    env.filters.update(fmtu=lambda u: f"{float(u):g}")
    env.globals.update(stake=grade.stake, staked_pl=grade.staked_pl, unit_names=live.UNIT_NAMES)
    return env


def _tip_label(p):
    try:
        return datetime.fromisoformat(p["tip_utc"]).astimezone(ET).strftime("%a %-I:%M %p ET")
    except Exception:  # noqa: BLE001
        return ""


def _decorate(p, now):
    p = dict(p)
    p["badge"] = teams.badge(p["team"])
    p["tip_label"] = _tip_label(p)
    try:
        p["locked"] = now >= datetime.fromisoformat(p["tip_utc"])
    except Exception:  # noqa: BLE001
        p["locked"] = True
    return p


def _corr_warning(picks):
    if len(picks) < 2:
        return None
    by_game, by_team = defaultdict(list), defaultdict(list)
    for p in picks:
        by_game[p["game_id"]].append(p)
        by_team[p["team"]].append(p)
    msgs = []
    for ps in by_team.values():
        if len(ps) >= 2:
            msgs.append(f"{', '.join(p['player'] for p in ps)} play for {ps[0]['team']}: their results tend to move together")
    for ps in by_game.values():
        tms = {p["team"] for p in ps}
        if len(ps) >= 2 and len(tms) > 1:
            msgs.append(f"{', '.join(p['player'] for p in ps)} are in the same game ({ps[0]['team']} vs {ps[0]['opp']}): pace and blowouts affect both")
    return "Correlated picks: " + "; ".join(msgs) + "." if msgs else None


def _notices(ctx, stat_keys=None):
    src = ctx["sources"]
    out = []
    if not ctx["season"]["days_loaded"]:
        out.append("<strong>No box scores from this season yet.</strong> Projections use the last two seasons until games are played; the first box scores arrive the morning after opening night.")
    if src["prizepicks"]["error"]:
        out.append(f"<strong>PrizePicks unavailable:</strong> {src['prizepicks']['error']}")
    if src["kalshi"]["error"]:
        out.append(f"<strong>Kalshi unavailable:</strong> {src['kalshi']['error']}")
    if src["injuries"]["error"]:
        out.append(f"<strong>Injury report:</strong> {src['injuries']['error']}")
    if src["rosters"]["error"] and src["rosters"]["used"] != "ESPN roster":
        out.append("<strong>Rosters:</strong> ESPN roster pull unavailable, so each player is listed with his team from his last box score. Offseason moves may be missing.")
    if stat_keys and "pts" in stat_keys and not src["fanduel"]["props_at"]:
        out.append("FanDuel points props haven't been pulled yet (needs the ODDS_API_KEY secret; pulled once a day for that night's games).")
    return out


def _empty_best_text(ctx):
    if not ctx["slate"]["games"]:
        return "No upcoming games."
    opens = _freeze_opens(ctx)
    if opens:
        return (f"Picks show up as soon as lines are posted (up to {int(predictions.FREEZE_WINDOW.total_seconds() // 3600)} hours before tip) "
                f"and keep updating with line moves and injury news until each game tips. The first ones arrive around {opens}. "
                "Projections for every player are already up on Players and Picks \u2192 All Projections.")
    n = sum(ctx["sources"]["line_counts"].values())
    if not n:
        return ("No player lines are posted for this slate yet. Kalshi and PrizePicks usually list player props on game day; "
                "picks appear here (and freeze) the first build after a line shows up.")
    return "No picks qualify yet."


def _freeze_opens(ctx, g=None) -> str | None:
    """When the freeze window opens for a game (or the slate's first game), as 'h:mm AM ET (h:mm AM CT)';
    None once it's already open."""
    now = ctx["now"]
    tips = [g["tip_utc"]] if g else [x["tip_utc"] for x in ctx["slate"]["games"]]
    tips = [t for t in tips if t]
    if not tips:
        return None
    t = min(tips) - predictions.FREEZE_WINDOW
    if now >= t:
        return None
    et = t.astimezone(ET)
    ct = t.astimezone(ZoneInfo("America/Chicago"))
    day = "" if et.date() == now.astimezone(ET).date() else et.strftime("%a ")
    return f"{day}{et.strftime('%-I:%M %p')} ET ({ct.strftime('%-I:%M %p')} CT)"


# ---------------------------------------------------------------------------
# Card details (NFL-style): recent games vs the line, factor tiles, vs opponent
# ---------------------------------------------------------------------------

def _games_index(box) -> dict:
    """{player_id: [game dicts, oldest first]} for regular-season games played."""
    import polars as pl
    if box is None or not box.height:
        return {}
    played = model._with_combos(box.filter((pl.col("season_type") == model.REGULAR) & pl.col("played")))
    cols = ["player_id", "game_id", "date", "season", "opp", "team", "home", "min"] + list(model.STATS)
    idx: dict = {}
    for r in played.select(cols).sort("date").iter_rows(named=True):
        idx.setdefault(r["player_id"], []).append(r)
    return idx


def _graded_lines(preds) -> dict:
    return {(p["player"], p["stat"], p["game_id"]): p["line"] for p in preds
            if p.get("status") in ("graded", "push") and p.get("line") is not None}


def _status(v, line):
    if line is None:
        return "push"
    return "over" if v > line else "under" if v < line else "push"


def card_details(p: dict, gidx: dict, graded: dict, prow: dict | None, n_recent: int = 10) -> dict:
    s, line = p["stat"], p.get("line")
    games = gidx.get(p.get("player_id"), [])
    recent = games[-n_recent:]
    bars = []
    side = p.get("call") or "OVER"

    def won(v, ln):
        st = _status(v, ln)
        if st == "push":
            return "push"
        return "over" if (st == "over") == (side == "OVER") else "under"   # over = green, under = red

    for g in recent:
        thr = graded.get((p["player"], s, g["game_id"]))
        bars.append({"label": f"{g['date'].month}/{g['date'].day}", "value": g[s],
                     "threshold": thr, "status": won(g[s], thr if thr is not None else line)})
    bars.append({"label": "PROJ", "value": p.get("projected"), "threshold": None, "status": "projection"})
    from dataio import CURRENT_SEASON
    cur = [g[s] for g in games if g["season"] == CURRENT_SEASON]
    if cur:
        base, base_label = sum(cur) / len(cur), f"{len(cur)}-game avg this season"
    else:
        last_season = max((g["season"] for g in games), default=None)
        prev = [g[s] for g in games if g["season"] == last_season]
        base = sum(prev) / len(prev) if prev else None
        base_label = f"{_season_label(last_season)} avg ({len(prev)} games)" if prev else None
    rec_vals = [g[s] for g in recent]
    over_n = sum(1 for v in rec_vals if line is not None and won(v, line) == "over")
    vs = [g[s] for g in games if g["opp"] == p.get("opp") and g["season"] >= CURRENT_SEASON - 2]
    f = (prow or {}).get("factors") or {}
    tiles = []
    if prow:
        tiles.append({"v": f"{prow['proj_min']:.1f}", "l": f"proj min (recent {f.get('min_recent', 0):.1f})", "css": "neutral"})
    if base is not None:
        tiles.append({"v": _fmtline(round(base, 1)), "l": base_label, "css": "neutral"})
    if rec_vals:
        avg10 = sum(rec_vals) / len(rec_vals)
        tiles.append({"v": _fmtline(round(avg10, 1)), "l": f"last {len(rec_vals)} avg",
                      "css": "neutral" if line is None else "good" if won(avg10, line) == "over" else "bad"})
    if p.get("var"):
        tiles.append({"v": f"±{p['var'] ** 0.5:.1f}", "l": "typical miss (1 sd)", "css": "neutral"})
    for key, lab in (("pace", "pace"), ("home", "home/road"), ("rest", "rest")):
        if f.get(key) is not None:
            x = f[key]
            tiles.append({"v": f"{x:.2f}×", "l": lab, "css": "good" if x > 1.005 else "bad" if x < 0.995 else "neutral"})
    tm = (f.get("team") or {}).get(s)
    if tm is not None:
        tiles.append({"v": f"{tm:.2f}×", "l": "shared production", "css": "good" if tm > 1.005 else "bad" if tm < 0.995 else "neutral"})
    return {
        "chart": svgchart.bar_chart_with_threshold(bars, line=line, baseline=base,
                                                   label=f"{p['player']} {model.STAT_LABEL[s]}, last {len(recent)} games vs the line"),
        "recent_n": len(rec_vals), "recent_over": over_n,
        "baseline": round(base, 1) if base is not None else None, "baseline_label": base_label,
        "tiles": tiles,
        "vs_opp": {"n": len(vs), "avg": round(sum(vs) / len(vs), 1), "best": max(vs),
                   "over": sum(1 for v in vs if line is not None and won(v, line) == "over")} if vs else None,
    }


DAY_GROUPS = [("pts", "PTS", ("pts",)), ("reb", "REB", ("reb",)), ("ast", "AST", ("ast",)),
              ("fg3m", "3PM", ("fg3m",)), ("combos", "PRA+", ("pra", "pr", "pa", "ra"))]


def _rec(ps):
    g = [p for p in ps if p.get("status") == "graded"]
    h = sum(1 for p in g if p["hit"])
    return {"hits": h, "misses": len(g) - h, "graded": len(g), "rate": round(100 * h / len(g), 1) if g else None,
            "pending": sum(1 for p in ps if p.get("status") == "pending"),
            "void": sum(1 for p in ps if p.get("status") == "void"), "push": sum(1 for p in ps if p.get("status") == "push"),
            "units": round(sum(grade.units(p) or 0 for p in g), 2)}


def _finals(box) -> dict:
    """{date_iso: [{game_id, away, home, away_score, home_score}]} from box scores."""
    import polars as pl
    if box is None or not box.height:
        return {}
    rows = (box.filter(pl.col("home") & pl.col("team_score").is_not_null())
            .select("game_id", "date", "team", "opp", "team_score", "opp_score").unique(subset=["game_id"]))
    out = {}
    for r in rows.iter_rows(named=True):
        out.setdefault(r["date"].isoformat(), []).append({"game_id": r["game_id"], "home": r["team"], "away": r["opp"],
                                                           "home_score": int(r["team_score"]), "away_score": int(r["opp_score"])})
    return out


def day_groups(picks: list, tpicks: list, finals: list | None = None) -> list:
    """One History-day tab per stat group (plus Games): its record, and its
    picks split by game in tip order."""
    games = {}
    for p in picks:
        k = p["game_id"]
        if k not in games:
            away, home = (p["opp"], p["team"]) if p["home"] else (p["team"], p["opp"])
            games[k] = {"game_id": k, "away": away, "home": home, "tip": p.get("tip_utc") or "",
                        "away_badge": teams.badge(away), "home_badge": teams.badge(home)}
    order = sorted(games.values(), key=lambda g: (g["tip"], g["away"]))
    out = []
    for key, label, stats in DAY_GROUPS:
        ps = [p for p in picks if p["stat"] in stats]
        if not ps:
            continue
        rows = []
        for g in order:
            gp = sorted([p for p in ps if p["game_id"] == g["game_id"]],
                        key=lambda p: ({"graded": 0, "push": 1, "void": 2}.get(p["status"], 3), not p.get("hit"), p["player"], p["stat"]))
            if gp:
                rows.append({**g, "picks": gp, "rec": _rec(gp)})
        out.append({"key": key, "label": label, "rec": _rec(ps), "games": rows,
                    "best5": _rec([p for p in ps if any(t.endswith(":best5") for t in p.get("best5", []))])})
    # Games tab: every game of the day with its final, and our moneyline / spread / total picks when we had them
    gm = {}
    for f in finals or []:
        gm[f["game_id"]] = {**f, "picks": []}
    for g in order:
        gm.setdefault(g["game_id"], {"game_id": g["game_id"], "away": g["away"], "home": g["home"], "picks": []})
    for t in tpicks:
        gm.setdefault(t["game_id"], {"game_id": t["game_id"], "away": t["away"], "home": t["home"], "picks": []})["picks"].append(t)
    if gm:
        mk = {"moneyline": 0, "spread": 1, "total": 2}
        rows = []
        for v in gm.values():
            v["picks"].sort(key=lambda t: mk.get(t["market"], 9))
            v.update(away_badge=teams.badge(v["away"]), home_badge=teams.badge(v["home"]), rec=_rec(v["picks"]),
                     tip=next((g["tip"] for g in order if g["game_id"] == v["game_id"]), "")
                     or next((t.get("tip_utc") or "" for t in v["picks"]), ""))
            rows.append(v)
        rows.sort(key=lambda v: (v["tip"], v["away"]))
        out.append({"key": "games", "label": "GAMES", "rec": _rec(tpicks), "gamerows": rows, "has_picks": bool(tpicks),
                    "best5": _rec([t for t in tpicks if "game:best5" in t.get("best5", [])])})
    return out


def _avg(vals):
    return round(sum(vals) / len(vals), 1) if vals else None


def player_page(r: dict, g: dict, gidx: dict, ppreds: list) -> dict:
    """Everything the player page needs: per-stat charts vs tonight's line,
    splits, game log (with our graded picks marked) and our record on him."""
    from dataio import CURRENT_SEASON
    games = gidx.get(r["player_id"], [])
    graded_by = {(p["game_id"], p["stat"]): p for p in ppreds if p.get("status") in ("graded", "push")}
    charts, splits = {}, []
    for s in BOX_STATS:
        st = r["stats"][s]
        line = (st.get("pick") or {}).get("line")
        if line is None and st.get("frozen"):
            line = st["frozen"]["line"]
        ref = line if line is not None else st["proj"]
        bars = []
        for gg in games[-15:]:
            gp = graded_by.get((gg["game_id"], s))
            v = gg[s]
            bars.append({"label": f"{gg['date'].month}/{gg['date'].day}", "value": v,
                         "threshold": gp["line"] if gp else None,
                         "status": "over" if v > ref else "under" if v < ref else "push"})
        bars.append({"label": "PROJ", "value": st["proj"], "threshold": None, "status": "projection"})
        cur = [gg[s] for gg in games if gg["season"] == CURRENT_SEASON]
        last = [gg[s] for gg in games if gg["season"] == CURRENT_SEASON - 1]
        base = _avg(cur) if cur else _avg(last)
        charts[s] = {"svg": svgchart.bar_chart_with_threshold(bars, line=line, baseline=base,
                                                              label=f"{r['player']} {model.STAT_LABEL[s]}, last {len(bars) - 1} games"),
                     "line": line, "n": len(bars) - 1,
                     "over_n": sum(1 for b in bars[:-1] if line is not None and b["value"] > line)}
        l3 = [gg for gg in games if gg["season"] >= CURRENT_SEASON - 2]
        splits.append({"stat": s, "proj": st["proj"], "line": line,
                       "season": _avg(cur), "season_n": len(cur), "last": _avg(last), "last_n": len(last),
                       "l5": _avg([gg[s] for gg in games[-5:]]), "l15": _avg([gg[s] for gg in games[-15:]]),
                       "home": _avg([gg[s] for gg in l3 if gg["home"]]), "away": _avg([gg[s] for gg in l3 if not gg["home"]]),
                       "vs": _avg([gg[s] for gg in l3 if gg["opp"] == r["opp"]]), "vs_n": sum(1 for gg in l3 if gg["opp"] == r["opp"])})
    log = []
    for gg in reversed(games[-20:]):
        cells = {}
        for s in BOX_STATS:
            gp = graded_by.get((gg["game_id"], s))
            css = ""
            if gp and gp.get("status") == "graded":
                css = "stat-good" if gp["hit"] else "stat-bad"
            cells[s] = {"v": gg[s], "css": css, "pick": f"{'o' if gp['call'] == 'OVER' else 'u'}{_fmtline(gp['line'])}" if gp else None}
        log.append({"date": gg["date"].strftime("%b %-d"), "season": gg["season"], "opp": gg["opp"], "home": gg["home"],
                    "min": int(round(gg["min"] or 0)), "cells": cells})
    gr = [p for p in ppreds if p.get("status") == "graded"]
    money = grade._money_row(ppreds) if ppreds else None
    recent = sorted(gr, key=lambda p: (p["date"], p["stat"]))[-10:]
    return {"charts": charts, "splits": splits, "log": log, "n_games": len(games),
            "record": {"n": len(gr), "hits": sum(bool(p["hit"]) for p in gr),
                       "rate": round(100 * sum(bool(p["hit"]) for p in gr) / len(gr), 1) if gr else None,
                       "money": money, "pending": sum(1 for p in ppreds if p.get("status") == "pending"),
                       "recent": [{"s": "over" if p["hit"] else "under", "v": f"{STAT_SHORT.get(p['stat'], p['stat'])}"} for p in recent]}}


def recent_strip(gidx: dict, player_id, stat: str, line, side: str | None = None, n: int = 10) -> list:
    """Last n games; colored green when the pick (side) would have won."""
    games = gidx.get(player_id, [])[-n:]
    out = []
    for g in games:
        st = _status(g[stat], line)
        if st != "push" and side == "UNDER":
            st = "over" if st == "under" else "under"
        out.append({"v": g[stat], "s": st})
    return out


# ---------------------------------------------------------------------------
# History charts (MLB-style)
# ---------------------------------------------------------------------------

HISTORY_GROUPS = [("pts", "Points", ("pts",)), ("reb", "Rebounds", ("reb",)), ("ast", "Assists", ("ast",)),
                  ("fg3m", "3-Pointers", ("fg3m",)), ("combos", "PRA & combos", ("pra", "pr", "pa", "ra"))]


def _date_label(d):
    return datetime.strptime(d, "%Y-%m-%d").strftime("%-m/%-d")


def history_charts(preds: list, health: list) -> dict:
    graded = [p for p in preds if p.get("status") == "graded"]
    dates = sorted({p["date"] for p in graded})
    out = {"n_days": len(dates)}
    xl = (_date_label(dates[0]), _date_label(dates[-1])) if dates else None

    # accuracy over time, one small chart per group
    acc = []
    for key, name, stats in HISTORY_GROUPS:
        pts, total, hits, be = [], 0, 0, []
        for d in dates:
            day = [p for p in graded if p["date"] == d and p["stat"] in stats]
            pts.append(round(100 * sum(bool(p["hit"]) for p in day) / len(day), 1) if day else None)
            total += len(day)
            hits += sum(bool(p["hit"]) for p in day)
            be += [p["break_even"] for p in day if p.get("break_even") is not None]
        be_avg = round(100 * sum(be) / len(be), 1) if be else None
        refs = [{"y": 50, "color": "#ab9cc8", "label": "50%", "below": True, "left": True}]
        if be_avg:
            refs.append({"y": be_avg, "color": "#ff6a13", "label": f"break-even {be_avg}%"})
        acc.append({"name": name, "n": total, "rate": round(100 * hits / total, 1) if total else None, "be": be_avg,
                    "chart": svgchart.line_chart([{"points": pts, "color": "#ffc531"}], height=170, references=refs,
                                                 x_labels=xl, y_suffix="%", label=f"{name} daily hit rate")
                    if sum(1 for x in pts if x is not None) >= 2 else None})
    out["accuracy"] = acc

    # stated chance vs actual, by day
    said, got = [], []
    for d in dates:
        day = [p for p in graded if p["date"] == d and p.get("chance") is not None]
        said.append(round(100 * sum(p["chance"] for p in day) / len(day), 1) if day else None)
        got.append(round(100 * sum(bool(p["hit"]) for p in day) / len(day), 1) if day else None)
    out["calibration_chart"] = svgchart.line_chart(
        [{"points": got, "color": "#ffc531"}, {"points": said, "color": "#4f86ff"}],
        height=200, references=[{"y": 50, "color": "#ab9cc8"}], x_labels=xl, y_suffix="%",
        label="stated chance vs actual hit rate by day") if len(dates) >= 2 else None

    # does value (chance minus break-even) mean anything?
    vb = []
    for lo, hi, lab in ((0, 3, "0-3 pts"), (3, 6, "3-6 pts"), (6, 10, "6-10 pts"), (10, 101, "10+ pts")):
        b = [p for p in graded if p.get("break_even") is not None and p.get("chance") is not None
             and lo <= 100 * (p["chance"] - p["break_even"]) < hi]
        vb.append({"bucket": lab, "n": len(b), "rate": round(100 * sum(bool(p["hit"]) for p in b) / len(b), 1) if b else None})
    out["value_buckets"] = vb

    # Best 5 / Best Value track records, day by day
    tracks = []
    for key, name, _stats in HISTORY_GROUPS:
        for lst, lst_name in (("best5", "Best 5 (highest chance)"), ("value", "Best Value")):
            tag = f"{key}:{lst}"
            tagged = [p for p in preds if tag in (p.get("best5") or [])]
            if not tagged:
                continue
            days = sorted({p["date"] for p in tagged}, reverse=True)
            rows, series, tot_n, tot_h = [], [], 0, 0
            for d in days:
                day = sorted([p for p in tagged if p["date"] == d], key=lambda p: -p.get("chance", 0))
                g = [p for p in day if p["status"] == "graded"]
                h = sum(bool(p["hit"]) for p in g)
                tot_n, tot_h = tot_n + len(g), tot_h + h
                rows.append({"date": d, "hits": h, "graded": len(g), "picks": [
                    {"player": p["player"], "call": p["call"], "line": p["line"], "stat": p["stat"],
                     "mark": "hit" if p["status"] == "graded" and p["hit"] else "miss" if p["status"] == "graded"
                     else p["status"]} for p in day]})
            for d in reversed(days):
                r = next(x for x in rows if x["date"] == d)
                series.append(round(100 * r["hits"] / r["graded"], 1) if r["graded"] else None)
            tracks.append({"name": f"{name}: {lst_name}", "n": tot_n, "hits": tot_h,
                           "rate": round(100 * tot_h / tot_n, 1) if tot_n else None, "rows": rows,
                           "chart": svgchart.line_chart([{"points": series, "color": "#3dff8f"}], height=150,
                                                        references=[{"y": 50, "color": "#ff6a13", "label": "50%"}],
                                                        x_labels=(_date_label(days[-1]), _date_label(days[0])),
                                                        y_suffix="%", label=f"{name} {lst_name} daily hit rate")
                           if sum(1 for x in series if x is not None) >= 2 else None})
    out["tracks"] = tracks

    # pipeline health: lines matched per source, per build
    h = health[-80:]
    if len(h) >= 2:
        srcs = [("prizepicks", "#ff6a13"), ("kalshi", "#4f86ff"), ("fanduel", "#3dff8f")]
        ymax = max([max((x.get("lines") or {}).values(), default=0) for x in h] + [10])
        out["health_chart"] = svgchart.line_chart(
            [{"points": [(x.get("lines") or {}).get(sname, 0) for x in h], "color": c} for sname, c in srcs],
            height=190, y_max=round(ymax * 1.1), x_labels=(h[0]["at"][5:16], h[-1]["at"][5:16]),
            label="player lines matched per build, by source")
    else:
        out["health_chart"] = None
    return out


def render_all(ctx: dict) -> None:
    env = _env()
    now = ctx["now"]
    common = {"nav_pages": NAV, "nav_icon": NAV_ICON, "build_time": now.strftime("%Y-%m-%d %H:%M UTC"),
              "cache_bust": _css_hash()}
    DOCS.mkdir(exist_ok=True)
    (DOCS / ".nojekyll").touch()
    sl, res, edge = ctx["slate"], ctx["res"], ctx["edge"]
    bt_all = (ctx["fit"]["backtest"].get("test_2026") or {})
    bt = {s: v for s, v in (bt_all.get("prop_relevant") or {}).items() if s in ("pts", "reb", "ast", "fg3m", "pra")}
    written = {}

    def write(name, template, slug, only_if_changed=False, **kw):
        html = env.get_template(template).render(current_page=slug, **{**common, **kw})
        f = DOCS / name
        if only_if_changed and f.exists() and f.read_text() == html:
            return
        f.write_text(html)
        written[name] = len(html)

    rk = ctx.get("rankings") or {}
    games_by_id = {g["game_id"]: g for g in sl["games"]}
    gidx = _games_index(ctx.get("box"))
    graded_lines = _graded_lines(ctx["preds"])
    prow_by = {(r["player"], r["game_id"]): r for r in res["players"]}
    best = {grp: {k: [{**(d := _decorate(p, now)), **card_details(d, gidx, graded_lines, prow_by.get((p["player"], p["game_id"]))),
                       "recent": recent_strip(gidx, p.get("player_id"), p["stat"], p.get("line"), p.get("call"))}
                      for p in v] for k, v in lists.items()} for grp, lists in ctx["best"].items()}

    # ---- per-player rows for every stat (Players page)
    def player_rows(g, stat_keys):
        rows = []
        for r in res["players"]:
            if r["game_id"] != g["game_id"]:
                continue
            stats, any_line = {}, False
            for s in stat_keys:
                st = dict(r["stats"][s])
                st["lines"] = {}
                for src, ln in r["stats"][s]["lines"].items():
                    ln = dict(ln)
                    ln["move"] = logs.movement(ctx["lm"], r["player"], s, r["game_id"], src)
                    st["lines"][src] = ln
                st["ladder"] = (r["stats"][s]["lines"].get("kalshi") or {}).get("ladder")
                st["frozen"] = predictions.get(g["date"], r["player"], s, r["game_id"])
                st["matchup"] = rankings.matchup(rk, r["opp"], r.get("pos"), s)
                st["recent"] = (recent_strip(gidx, r["player_id"], s, st["pick"]["line"], st["pick"]["side"])
                                if st.get("pick") else [])
                any_line = any_line or bool(st["lines"])
                stats[s] = st
            if r["proj_min"] < 10 and not any_line:
                continue
            rows.append({**r, "badge": teams.badge(r["team"]), "stats": stats, "any_line": any_line})
        rows.sort(key=lambda x: -x["proj_min"])
        return rows

    pgames = []
    for g in sl["games"]:
        rows = player_rows(g, ALL_STATS)
        pgames.append({"g": g, "teams": [{"badge": teams.badge(tm), "name": teams.full_name(tm),
                                          "rows": [r for r in rows if r["team"] == tm]} for tm in (g["away"], g["home"])]})
    write("players.html", "players.html", "players", slate=sl, edge=edge, games=pgames, box_stats=BOX_STATS,
          search_index=sorted({(r["player"], r["player_id"], r["team"]) for gm in pgames for tm in gm["teams"] for r in tm["rows"]}),
          combo_stats=["pr", "pa", "ra"], notices=_notices(ctx, ["pts"]), no_history=res["no_history"])

    # ---- player pages (everyone on the current slate)
    by_player_preds = defaultdict(list)
    for p in ctx["preds"]:
        by_player_preds[p.get("player_id")].append(p)
    keep_p = set()
    for gm in pgames:
        for tm in gm["teams"]:
            for r in tm["rows"]:
                pc = player_page(r, gm["g"], gidx, by_player_preds.get(r["player_id"], []))
                name = f"p-{r['player_id']}.html"
                keep_p.add(name)
                write(name, "player.html", "players", only_if_changed=True, r=r, g=gm["g"], pc=pc, edge=edge,
                      box_stats=BOX_STATS, combo_stats=["pr", "pa", "ra"], build_time=None)
    for f in DOCS.glob("p-*.html"):
        if f.name not in keep_p:
            f.unlink()

    # ---- picks (all stat groups on one page)
    groups = []
    for slug, title, fname, stat_keys, grp, lede in STAT_PAGES:
        b5 = (best.get(grp) or {}).get("best5", [])
        bv = (best.get(grp) or {}).get("value", [])
        groups.append({"key": grp, "short": GROUP_SHORT[grp], "title": title, "lede": lede, "stat_keys": stat_keys,
                       "best5": b5, "value": bv, "warn5": _corr_warning(b5), "warnv": _corr_warning(bv)})
    write("picks.html", "picks.html", "picks", slate=sl, edge=edge, groups=groups, notices=_notices(ctx, ["pts"]),
          empty_best=_empty_best_text(ctx), games=pgames, no_history=res["no_history"])

    # ---- games (moneyline + matchups + tracked game picks)
    tstore = predictions.load_date(sl["date"], team=True) if sl.get("date") else {}
    tf = ctx["fit"]["team"]
    inj = {}
    for p in (ctx.get("injuries") or {}).get("players", []):
        inj.setdefault(p.get("team"), []).append(p)
    gcards, gpicks_all = [], []
    by_gid = {g["game_id"]: g for g in sl["games"]}

    def deco_gp(p, g):
        q = dict(p)
        q.update(g=g, away_badge=g["away_badge"], home_badge=g["home_badge"], tip_et=g["tip_et"],
                 locked=g["started"], pick_badge=teams.badge(p["pick"]) if p["pick"] in teams.TEAMS else None,
                 frozen=bool(p.get("frozen_at")))
        return q

    for g in sl["games"]:
        tp = res["teams"].get(g["game_id"])
        frozen = {v["market"]: v for v in tstore.values() if v["game_id"] == g["game_id"]}
        live_p = {p["market"]: p for p in (g.get("game_picks") or [])}
        picks = []
        for mkt in ("moneyline", "spread", "total"):
            p = frozen.get(mkt) or live_p.get(mkt)
            if p:
                picks.append(deco_gp(p, g))
        gpicks_all += picks
        sides = []
        for tm in (g["away"], g["home"]):
            ps = [r for r in res["players"] if r["game_id"] == g["game_id"] and r["team"] == tm]
            ps.sort(key=lambda r: -r["stats"]["pts"]["proj"])
            sides.append({"badge": teams.badge(tm), "name": teams.full_name(tm), "players": ps[:8],
                          "out": [p["player"] for p in inj.get(tm, []) if p.get("class") == "out"],
                          "q": [p["player"] for p in inj.get(tm, []) if p.get("class") in ("questionable", "probable")]})
        gcards.append({"g": g, "fd": g.get("fanduel") or {}, "ml": (g.get("fanduel") or {}).get("moneyline"), "k": g.get("kalshi_ml"),
                       "tp": tp, "sides": sides, "picks": picks})
    g_best5 = sorted([deco_gp(v, by_gid[v["game_id"]]) for v in tstore.values()
                      if "game:best5" in v.get("best5", []) and v["game_id"] in by_gid], key=lambda p: -p["chance"])
    g_value = sorted([deco_gp(v, by_gid[v["game_id"]]) for v in tstore.values()
                      if "game:value" in v.get("best5", []) and v["game_id"] in by_gid], key=lambda p: -(p.get("value") or 0))
    g_mkt = []
    for mkt, label in (("moneyline", "Moneyline"), ("spread", "Spread"), ("total", "Total")):
        lists = {}
        for lst, key in (("best5", "chance"), ("value", "value")):
            lists[lst] = sorted([deco_gp(v, by_gid[v["game_id"]]) for v in tstore.values()
                                 if f"{mkt}:{lst}" in v.get("best5", []) and v["game_id"] in by_gid],
                                key=lambda p, key=key: -(p.get(key) or 0))
        n_open = sum(1 for p in gpicks_all if p["market"] == mkt)
        n_beat = sum(1 for p in gpicks_all if p["market"] == mkt and (p.get("value") or -1) >= 0)
        g_mkt.append({"key": mkt, "label": label, "n": n_open, "n_beat": n_beat, **lists})
    page_ids = {int(f[2:-5]) for f in keep_p}
    gmoney = grade.team_money(ctx["tpreds"])
    # data for the in-browser live tracker: tonight's frozen picks, keyed by game
    pstore = predictions.load_date(sl["date"]) if sl.get("date") else {}
    live_picks = defaultdict(list)
    for p in pstore.values():
        live_picks[str(p["game_id"])].append({"pid": str(p.get("player_id")), "player": p["player"], "team": p["team"], "stat": p["stat"],
                                              "call": p["call"], "line": p["line"], "chance": round(p["chance"], 3),
                                              "proj": p.get("projected"), "min": p.get("proj_min"), "u": grade.stake(p),
                                              "star": bool(p.get("best5"))})
    live_g = defaultdict(list)
    for t in tstore.values():
        live_g[str(t["game_id"])].append({"market": t["market"], "pick": t["pick"], "label": t.get("pick_label") or t["pick"],
                                          "line": t.get("line"), "home": t["home"], "away": t["away"], "chance": round(t["chance"], 3), "u": grade.stake(t),
                                          "star": bool(t.get("best5"))})
    live_data = {"date": (sl.get("date") or "").replace("-", ""),
                 "games": [{"gid": str(g["game_id"]), "away": g["away"], "home": g["home"], "tip": g["tip_et"],
                            "ac": g["away_badge"]["color"], "hc": g["home_badge"]["color"]} for g in sl["games"]],
                 "picks": live_picks, "gpicks": live_g}
    write("games.html", "games.html", "games", slate=sl, cards=gcards, page_ids=page_ids, team_fit=tf,
          g_best5=g_best5, g_value=g_value, g_mkt=g_mkt, gmoney=gmoney, ed=ctx.get("edges") or {}, eds=ctx.get("edges_sum") or {}, blend_w=live.GAME_BLEND_W, cap=live.GAME_CHANCE_CAP, live_data=live_data,
          empty_best=_empty_best_text(ctx),
          notices=_notices(ctx)[:1] + ([f"<strong>Kalshi:</strong> {ctx['sources']['kalshi']['error']}"] if ctx['sources']['kalshi']['error'] else []))

    # ---- old URLs -> new pages
    redirect_t = env.from_string(
        '<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Moved | Prop Edge NBA</title>'
        '<meta http-equiv="refresh" content="0; url={{ to }}"><link rel="canonical" href="{{ to }}">'
        '<script>location.replace("{{ to }}");</script></head>'
        '<body style="background:#130b22;color:#fff3e3;font-family:monospace;padding:24px">'
        'This page moved: <a style="color:#ffc531" href="{{ to }}">{{ to }}</a></body></html>')
    for old, to in REDIRECTS.items():
        (DOCS / old).write_text(redirect_t.render(to=to))

    # ---- history
    preds = ctx["preds"]
    months = defaultdict(list)
    for p in preds:
        months[p["date"][:7]].append(p)
    tmonths = defaultdict(list)
    for t in ctx["tpreds"]:
        tmonths[t["date"][:7]].append(t)
    mlist = [{"key": k, "label": datetime.strptime(k, "%Y-%m").strftime("%b %Y"), "n": len(v)}
             for k, v in sorted(months.items(), reverse=True)]
    b5rec = []
    rec_rows = defaultdict(lambda: {"n": 0, "hits": 0, "pending": 0})
    for p in preds:
        for tag in p.get("best5", []):
            grp, lst = tag.split(":")
            rr = rec_rows[(grp, lst)]
            if p["status"] == "graded":
                rr["n"] += 1
                rr["hits"] += bool(p["hit"])
            elif p["status"] == "pending":
                rr["pending"] += 1
    for (grp, lst), rr in sorted(rec_rows.items()):
        b5rec.append({"page": GROUP_PAGE.get(grp, grp), "list_label": "Best 5" if lst == "best5" else "Best Value",
                      "n": rr["n"], "pending": rr["pending"],
                      "hit_rate": round(100 * rr["hits"] / rr["n"], 1) if rr["n"] else None})
    try:
        retune_log = list(reversed(json.loads((ROOT / "data" / "retune_log.json").read_text())))[:12]
    except Exception:  # noqa: BLE001
        retune_log = []
    hc = history_charts(preds, ctx["health"])
    money = grade.money_summary(preds)
    if len(money["series"]) >= 2:
        lo, hi = min(money["series"] + [0]), max(money["series"] + [0])
        pad = max(1.0, (hi - lo) * 0.15)
        money["chart"] = svgchart.line_chart([{"points": money["series"], "color": "#3dff8f" if money["series"][-1] >= 0 else "#ff3d57"}],
                                             height=200, y_min=lo - pad, y_max=hi + pad,
                                             references=[{"y": 0, "color": "#ab9cc8", "label": "break even", "left": True, "below": True}],
                                             x_labels=(_date_label(money["dates"][0]), _date_label(money["dates"][-1])),
                                             label="Running units, 1 unit per graded pick")
    else:
        money["chart"] = None
    day_index = []
    for d in sorted({p["date"] for p in preds}, reverse=True):
        dp = [p for p in preds if p["date"] == d]
        day_index.append({"date": d, "label": datetime.strptime(d, "%Y-%m-%d").strftime("%a %b %-d"), "n": len(dp), **_rec(dp),
                          "rec": round(sum(grade.staked_pl(p) or 0 for p in dp), 2),
                          "games": _rec([t for t in ctx["tpreds"] if t["date"] == d])})
    try:
        audit = json.loads((ROOT / "data" / "audit.json").read_text())
    except Exception:  # noqa: BLE001
        audit = None
    hv = history_view.build(preds, ctx["tpreds"], ctx["health"], keep_p, ctx.get("trust"))
    write("history.html", "history.html", "history", hv=hv, pat=ctx.get("patterns") or {}, espn=ctx.get("espn"), hc=hc, money=money, day_index=day_index, audit=audit, gmoney=grade.team_money(ctx["tpreds"]), months=mlist, retune_log=retune_log, tuning=ctx.get("tuning") or {}, lineups=ctx.get("lineups") or {}, scoreboard=grade.stat_scoreboard(preds), edge=edge,
          buckets=grade.chance_buckets(preds), best5_rec=b5rec, team_rec=grade.team_record(ctx["tpreds"]),
          maturity=grade.maturity(ctx["cal"]), bt=bt, bt_cal=bt_all.get("calibration") or {},
          bt_n=(bt.get("pts") or {}).get("n"), health=list(reversed(ctx["health"][-30:])),
          n_pending=sum(1 for p in preds if p["status"] == "pending"))
    write("rankings.html", "rankings.html", "rankings", rk=rk)
    pz = ctx.get("parlays") or {}
    for d in pz.get("days") or []:
        for r in d["rows"]:
            r["first_tip_label"] = _tip_label({"tip_utc": r.get("first_tip")})
            for l in r["legs"]:
                l["badge"] = teams.badge(l["team"] if l["kind"] == "player" else (l["pick"] if l["pick"] in teams.TEAMS else l["home"]))
                l["tip_label"] = _tip_label(l)
    today = next((d for d in pz.get("days") or [] if d["date"] == sl.get("date")), None)
    write("parlays.html", "parlays.html", "parlays", pz=pz, today=today,
          fam_label={f["key"]: f["label"] for f in pz.get("families") or []})
    keep = set()
    finals_by_date = _finals(ctx.get("box"))
    for mo in mlist:
        ps = months[mo["key"]]
        days = defaultdict(list)
        for p in ps:
            q = dict(p)
            q["badge"] = teams.badge(p["team"])
            tags = [t.split(":")[1] for t in p.get("best5", [])]
            q["best5_label"] = " + ".join("Best 5" if t == "best5" else "Best Value" for t in tags)
            days[p["date"]].append(q)
        tdays = defaultdict(list)
        for t in tmonths.get(mo["key"], []):
            tdays[t["date"]].append(t)
        day_list = []
        for d, v in sorted(days.items(), reverse=True):
            gd = [x for x in v if x["status"] == "graded"]
            b5 = [x for x in gd if x.get("best5")]
            day = {"date": d, "label": datetime.strptime(d, "%Y-%m-%d").strftime("%a %b %-d"),
                   "picks": sorted(v, key=lambda x: (x["stat"], x["player"])), "team": tdays.get(d, []),
                   "n": len(v), "graded": len(gd), "hits": sum(bool(x["hit"]) for x in gd),
                   "rate": round(100 * sum(bool(x["hit"]) for x in gd) / len(gd), 1) if gd else None,
                   "b5_graded": len(b5), "b5_hits": sum(bool(x["hit"]) for x in b5),
                   "void": sum(x["status"] == "void" for x in v), "pending": sum(x["status"] == "pending" for x in v)}
            day["groups"] = day_groups(v, tdays.get(d, []), finals_by_date.get(d))
            day_list.append(day)
            dname = f"history-{d}.html"
            keep.add(dname)
            # rendered every build but only written when something on it changed (e.g. grading landed)
            write(dname, "history_day.html", "history", only_if_changed=True, build_time=None,
                  day=day, month_key=mo["key"], month_label=mo["label"])
        g = [p for p in ps if p["status"] == "graded"]
        summ = {"graded": len(g), "hits": sum(bool(p["hit"]) for p in g),
                "hit_rate": round(100 * sum(bool(p["hit"]) for p in g) / len(g), 1) if g else None,
                "void": sum(p["status"] == "void" for p in ps), "push": sum(p["status"] == "push" for p in ps),
                "pending": sum(p["status"] == "pending" for p in ps)}
        name = f"history-{mo['key']}.html"
        write(name, "history_month.html", "history", month_key=mo["key"], month_label=mo["label"], months=mlist,
              days=day_list, summary=summ)
        keep.add(name)
    for f in DOCS.glob("history-*.html"):
        if f.name not in keep:
            f.unlink()

    # ---- home
    tops = []
    for grp in ("pts", "reb", "ast", "fg3m", "combos"):
        lst = (best.get(grp) or {}).get("best5") or []
        if lst:
            tops.append({"page": GROUP_PAGE[grp], "href": GROUP_HREF[grp], "p": lst[0]})
    top_all, seen = [], set()
    for p in sorted((p for grp in ("pts", "reb", "ast", "fg3m", "combos") for p in (best.get(grp) or {}).get("best5") or []),
                    key=lambda p: -p["chance"]):
        if p["player"] not in seen:
            seen.add(p["player"])
            top_all.append(p)
    top_all = top_all[:6]
    top_groups = [{"key": grp, "short": GROUP_SHORT[grp], "cards": (best.get(grp) or {}).get("best5") or []}
                  for grp in ("pts", "reb", "ast", "fg3m", "combos")]
    record = {"hits": sum(1 for p in preds if p["status"] == "graded" and p["hit"]),
              "misses": sum(1 for p in preds if p["status"] == "graded" and not p["hit"]),
              "pending": sum(1 for p in preds if p["status"] == "pending"),
              "void": sum(1 for p in preds if p["status"] == "void"), "push": sum(1 for p in preds if p["status"] == "push")}
    record["graded"] = record["hits"] + record["misses"]
    record["rate"] = round(100 * record["hits"] / record["graded"], 1) if record["graded"] else None
    record["money"] = grade.money_summary(preds)["total"]

    def day_board(date, label):
        ps = [p for p in preds if p["date"] == date]
        tps = [t for t in ctx["tpreds"] if t["date"] == date]
        gd = [p for p in ps if p["status"] == "graded"]
        h = sum(1 for p in gd if p["hit"])
        mon = grade._money_row(ps)
        tg = [t for t in tps if t["status"] == "graded"]
        return {"label": label, "date": date, "date_label": datetime.strptime(date, "%Y-%m-%d").strftime("%a %b %-d"),
                "hits": h, "misses": len(gd) - h, "pending": sum(1 for p in ps if p["status"] == "pending"),
                "void": sum(1 for p in ps if p["status"] == "void"), "push": sum(1 for p in ps if p["status"] == "push"),
                "rate": round(100 * h / len(gd), 1) if gd else None, "units": mon["units"], "clv_rate": mon["clv_rate"],
                "n": len(ps), "games_n": len(tps), "games_hits": sum(1 for t in tg if t["hit"]), "games_graded": len(tg)}
    pdates = sorted({p["date"] for p in preds})
    today = sl.get("date") or (pdates[-1] if pdates else None)
    prev = max((d for d in pdates if today and d < today), default=None)
    boards = []
    if today:
        boards.append(day_board(today, "TONIGHT"))
    if prev:
        boards.append(day_board(prev, "PREVIOUS DAY"))
    frozen_n = defaultdict(int)
    for p in preds:
        frozen_n[p["game_id"]] += 1
    tiles = []
    for g in sl["games"]:
        op = _freeze_opens(ctx, g)
        tiles.append({"g": g, "fd": g.get("fanduel") or {}, "tp": res["teams"].get(g["game_id"]), "n": frozen_n.get(g["game_id"], 0),
                      "opens": op.split(" (")[0].replace(" ET", " ET") if op else None})
    season = dict(ctx["season"])
    season["phase_label"] = {"preseason": "PRE", "in_season": "LIVE", "complete": "DONE"}.get(season["phase"], "—")
    src = ctx["sources"]
    box_st = src["box"].get(f"player_box_{2027}", {})
    sched_st = src["box"].get("schedule_2027", {})

    def row(name, ok, state, detail, warn=False):
        return {"name": name, "css": "status-ok" if ok else ("status-warn" if warn else "status-bad"), "state": state, "detail": detail}

    fd = src["fanduel"]
    usage = fd.get("usage") or {}
    source_rows = [
        row("Box scores (sportsdataverse)", box_st.get("state") == "ok",
            {"ok": "ok", "not_published": "not published yet", "error": "error"}.get(box_st.get("state"), "not checked"),
            f"upstream file updated {box_st.get('last_modified') or '—'}; checked {box_st.get('checked_at') or '—'}"
            + (" · the 2026-27 file isn't published until games are played" if box_st.get("state") == "not_published" else ""),
            warn=box_st.get("state") == "not_published"),
        row("Schedule", sched_st.get("state") == "ok", sched_st.get("state", "not checked").replace("_", " "),
            f"updated {sched_st.get('last_modified') or '—'}"),
        row("ESPN injuries", not src["injuries"]["error"], "ok" if not src["injuries"]["error"] else "using last copy",
            f"{src['injuries']['n']} listed · fetched {src['injuries']['at'] or 'never'}" + (f" · {src['injuries']['error']}" if src['injuries']['error'] else "")),
        row("ESPN rosters", src["rosters"]["used"] == "ESPN roster", src["rosters"]["used"],
            f"{src['rosters']['n']} players · fetched {src['rosters']['at'] or 'never'}", warn=True),
        row("Kalshi", not src["kalshi"]["error"], "ok" if not src["kalshi"]["error"] else "error",
            "; ".join(f"{k}: {v}" for k, v in src["kalshi"]["per_series"].items()) or (src["kalshi"]["error"] or "")),
        row("PrizePicks (experimental)", not src["prizepicks"]["error"], "ok" if not src["prizepicks"]["error"] else "error",
            f"{src['prizepicks']['n']} NBA lines" if not src["prizepicks"]["error"] else src["prizepicks"]["error"]),
        row("FanDuel (The Odds API)", bool(fd.get("lines_at")), "cached" if fd.get("lines_at") else "not pulled yet",
            (f"game lines {fd.get('lines_at') or '—'} · points props {fd.get('props_at') or '—'}"
             + (f" · credits left {usage.get('requests_remaining')}" if usage else "")
             + "".join(f" · {v}" for k, v in fd.items() if k.endswith("_error"))), warn=not fd.get("lines_at")),
    ]
    n_lines = sum(len(v) for v in res["picks"].values())
    write("index.html", "home.html", "home", season=season, slate=sl, n_players=len(res["players"]), n_lines=n_lines,
          n_graded=sum(1 for p in preds if p["status"] == "graded"), tops=tops, edge=edge, source_rows=source_rows,
          top_all=top_all, top_groups=top_groups, record=record, tiles=tiles, boards=boards,
          maturity=grade.maturity(ctx["cal"]), bt=bt,
          no_picks_reason=_empty_best_text(ctx))

    static_src = ROOT / "docs" / "static"
    static_src.mkdir(exist_ok=True)
    for name, size in sorted(written.items()):
        print(f"  wrote {name} ({size // 1024} KB)")
