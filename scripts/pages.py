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
import live
import logs
import markets
import model
import predictions
import teams

ROOT = Path(__file__).parent.parent
DOCS = ROOT / "docs"
TEMPLATES = Path(__file__).parent / "templates"
ET = ZoneInfo("America/New_York")

NAV = [
    ("home", "Home", "index.html"),
    ("moneyline", "Moneyline", "moneyline.html"),
    ("matchups", "Matchups", "matchups.html"),
    ("points", "Points", "points.html"),
    ("rebounds", "Rebounds", "rebounds.html"),
    ("assists", "Assists", "assists.html"),
    ("threes", "3-Pointers", "threes.html"),
    ("pra", "PRA", "pra.html"),
    ("history", "History", "history.html"),
]

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
GROUP_HREF = {"pts": "points.html", "reb": "rebounds.html", "ast": "assists.html", "fg3m": "threes.html", "combos": "pra.html"}


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


def _env():
    env = Environment(loader=FileSystemLoader(str(TEMPLATES)), autoescape=True, trim_blocks=True, lstrip_blocks=True)
    env.filters.update(fmtline=_fmtline, fmtpt=_fmtpt, fmtnum=_fmtnum, season_label=_season_label)
    env.globals.update(stat_labels=model.STAT_LABEL, stat_short=STAT_SHORT, src_labels=markets.SOURCE_LABEL)
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
    n = sum(ctx["sources"]["line_counts"].values())
    if not n:
        return ("No player lines are posted for this slate yet. Kalshi and PrizePicks usually list player props on game day; "
                "picks appear here (and freeze) the first build after a line shows up.")
    return "No picks qualify yet."


def render_all(ctx: dict) -> None:
    env = _env()
    now = ctx["now"]
    common = {"nav_pages": NAV, "build_time": now.strftime("%Y-%m-%d %H:%M UTC"),
              "cache_bust": str(int(now.timestamp()))}
    DOCS.mkdir(exist_ok=True)
    (DOCS / ".nojekyll").touch()
    sl, res, edge = ctx["slate"], ctx["res"], ctx["edge"]
    bt_all = (ctx["fit"]["backtest"].get("test_2026") or {})
    bt = {s: v for s, v in (bt_all.get("prop_relevant") or {}).items() if s in ("pts", "reb", "ast", "fg3m", "pra")}
    written = {}

    def write(name, template, slug, **kw):
        html = env.get_template(template).render(current_page=slug, **common, **kw)
        (DOCS / name).write_text(html)
        written[name] = len(html)

    games_by_id = {g["game_id"]: g for g in sl["games"]}
    best = {grp: {k: [_decorate(p, now) for p in v] for k, v in lists.items()} for grp, lists in ctx["best"].items()}

    # ---- stat pages
    for slug, title, fname, stat_keys, grp, lede in STAT_PAGES:
        games = []
        for g in sl["games"]:
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
                    any_line = any_line or bool(st["lines"])
                    stats[s] = st
                if r["proj_min"] < 10 and not any_line:
                    continue
                rows.append({**r, "stats": stats, "any_line": any_line})
            rows.sort(key=lambda x: -x["stats"][stat_keys[0]]["proj"])
            games.append({"g": g, "rows": rows})
        b5 = (best.get(grp) or {}).get("best5", [])
        bv = (best.get(grp) or {}).get("value", [])
        write(fname, "stat.html", slug, page_title=title, lede=lede, stat_keys=stat_keys, slate=sl, edge=edge,
              games=games, best5=b5, value=bv, warn_best5=_corr_warning(b5), warn_value=_corr_warning(bv),
              notices=_notices(ctx, stat_keys), no_history=res["no_history"], empty_best=_empty_best_text(ctx))

    # ---- moneyline
    tstore = predictions.load_date(sl["date"], team=True) if sl.get("date") else {}
    tf = ctx["fit"]["team"]
    ml_rows = []
    for g in sl["games"]:
        tp = res["teams"].get(g["game_id"])
        fr = tstore.get(f"{g['game_id']}|moneyline")
        pick = chance = None
        if fr:
            pick, chance = fr["pick"], fr["chance"]
        elif tp and g.get("game_picks_open"):
            pick = g["home"] if tp["home_win"] >= 0.5 else g["away"]
            chance = max(tp["home_win"], 1 - tp["home_win"])
        why = "No projection for this game."
        if tp:
            why = (f"Our score: {g['away']} {tp['away_pts']:.0f}, {g['home']} {tp['home_pts']:.0f} (sum of player projections "
                   f"× {tf['scale']}). Margin {tp['margin']:+.1f} for {g['home']}; with a {tf['margin_sd']}-point spread of misses "
                   f"that gives {g['home']} {tp['home_win'] * 100:.0f}% to win.")
            if fr:
                why += f" Frozen {fr['frozen_at']}, before tip."
        ml_rows.append({"g": g, "fd": (g.get("fanduel") or {}).get("moneyline"), "k": g.get("kalshi_ml"), "pick": pick,
                        "pick_badge": teams.badge(pick) if pick else None, "pick_chance": chance, "frozen": fr, "why": why})
    team_rec = {r["market"]: r for r in grade.team_record(ctx["tpreds"])}
    write("moneyline.html", "moneyline.html", "moneyline", rows=ml_rows, team_fit=tf, min_games=live.GAME_PICKS_MIN_GAMES, ml_record=team_rec["moneyline"],
          notices=_notices(ctx)[:1] + ([f"<strong>Kalshi:</strong> {ctx['sources']['kalshi']['error']}"] if ctx['sources']['kalshi']['error'] else []))

    # ---- matchups
    inj = {}
    for p in (ctx.get("injuries") or {}).get("players", []):
        inj.setdefault(p.get("team"), []).append(p)
    cards = []
    for g in sl["games"]:
        sides = []
        for tm in (g["away"], g["home"]):
            ps = [r for r in res["players"] if r["game_id"] == g["game_id"] and r["team"] == tm]
            ps.sort(key=lambda r: -r["stats"]["pts"]["proj"])
            sides.append({"badge": teams.badge(tm), "name": teams.full_name(tm), "players": ps[:8],
                          "out": [p["player"] for p in inj.get(tm, []) if p.get("class") == "out"],
                          "q": [p["player"] for p in inj.get(tm, []) if p.get("class") in ("questionable", "probable")]})
        cards.append({"g": g, "fd": g.get("fanduel") or {}, "tp": res["teams"].get(g["game_id"]), "sides": sides,
                      "team_frozen": [v for k, v in tstore.items() if v["game_id"] == g["game_id"] and v["market"] != "moneyline"]})
    write("matchups.html", "matchups.html", "matchups", slate=sl, cards=cards, min_games=live.GAME_PICKS_MIN_GAMES, notices=_notices(ctx)[:1])

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
    write("history.html", "history.html", "history", months=mlist, retune_log=retune_log, scoreboard=grade.stat_scoreboard(preds), edge=edge,
          buckets=grade.chance_buckets(preds), best5_rec=b5rec, team_rec=grade.team_record(ctx["tpreds"]),
          maturity=grade.maturity(ctx["cal"]), bt=bt, bt_cal=bt_all.get("calibration") or {},
          bt_n=(bt.get("pts") or {}).get("n"), health=list(reversed(ctx["health"][-30:])),
          n_pending=sum(1 for p in preds if p["status"] == "pending"))
    keep = set()
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
        day_list = [{"label": datetime.strptime(d, "%Y-%m-%d").strftime("%a %b %-d"),
                     "picks": sorted(v, key=lambda p: (p["stat"], p["player"])), "team": tdays.get(d, [])}
                    for d, v in sorted(days.items(), reverse=True)]
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
          maturity=grade.maturity(ctx["cal"]), bt=bt,
          no_picks_reason=_empty_best_text(ctx))

    static_src = ROOT / "docs" / "static"
    static_src.mkdir(exist_ok=True)
    for name, size in sorted(written.items()):
        print(f"  wrote {name} ({size // 1024} KB)")
