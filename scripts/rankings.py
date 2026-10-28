"""
Team rankings: standings, and what every defense allows, overall and to each
position (guards / forwards / centers), for every stat the site projects.

Computed from the same ESPN box scores the model uses, every build. Rank 1 =
allows the MOST (best matchup for an OVER). Small early-season samples are
shown with their game count; the model itself uses the shrunk version
(model._def_state / def_k), and the matchup line on each projection reads
from here.
"""

from __future__ import annotations

from collections import defaultdict

import polars as pl

import dataio
import model
import teams

STATS = ("pts", "reb", "ast", "fg3m", "pra", "pr", "pa", "ra")
POS = (("G", "guards"), ("F", "forwards"), ("C", "centers"))


def _season_rows(box: pl.DataFrame, season: int):
    played = model._played(box).filter(pl.col("season") == season)
    tg = model._team_games(box).filter(pl.col("season") == season)
    return played, tg


def standings(box: pl.DataFrame, season: int) -> list[dict]:
    played, tg = _season_rows(box, season)
    if not tg.height:
        return []
    home = (box.filter((pl.col("season") == season) & pl.col("played"))
            .group_by(["game_id", "team"]).agg(pl.col("home").first()))
    tg = tg.join(home, on=["game_id", "team"], how="left").sort(["team", "date"])
    out = []
    for team, g in tg.group_by("team"):
        team = team[0] if isinstance(team, tuple) else team
        g = g.sort("date")
        win = (g["pf"] > g["pa"]).to_list()
        hm = g["home"].to_list()
        n = len(win)
        streak, last = 0, None
        for w in reversed(win):
            if last is None or w == last:
                streak += 1
                last = w
            else:
                break
        last10 = win[-10:]
        poss = g["poss"].sum()
        opp_poss = g["opp_poss"].sum()
        out.append({
            "team": team, "badge": teams.badge(team), "name": teams.full_name(team), "g": n,
            "w": sum(win), "l": n - sum(win), "pct": round(sum(win) / n, 3) if n else 0,
            "home": f"{sum(1 for w, h in zip(win, hm) if w and h)}-{sum(1 for w, h in zip(win, hm) if not w and h)}",
            "road": f"{sum(1 for w, h in zip(win, hm) if w and not h)}-{sum(1 for w, h in zip(win, hm) if not w and not h)}",
            "l10": f"{sum(last10)}-{len(last10) - sum(last10)}",
            "streak": (("W" if last else "L") + str(streak)) if last is not None else "-",
            "ppg": round(g["pf"].mean(), 1), "opp_ppg": round(g["pa"].mean(), 1),
            "diff": round(g["pf"].mean() - g["pa"].mean(), 1),
            "pace": round(g["game_poss"].mean(), 1),
            "ortg": round(100 * g["pf"].sum() / poss, 1) if poss else None,
            "drtg": round(100 * g["pa"].sum() / opp_poss, 1) if opp_poss else None,
        })
    for r in out:
        r["net"] = round((r["ortg"] or 0) - (r["drtg"] or 0), 1)
    out.sort(key=lambda r: (-r["pct"], -r["net"]))
    for i, r in enumerate(out, 1):
        r["rank"] = i
    return out


def allowed(box: pl.DataFrame, season: int) -> dict:
    """{stat: {"rows": [...per team...], "league": {...}}}; each row has per game and per 100
    possessions overall and per position, plus ranks (1 = most allowed) and % vs league."""
    played, tg = _season_rows(box, season)
    if not played.height:
        return {}
    per_game = played.group_by(["game_id", "opp", "pos"]).agg([pl.col(s).sum() for s in STATS]).rename({"opp": "def"})
    poss = tg.select("game_id", pl.col("team").alias("def"), "game_poss")
    games = tg.group_by("team").agg(pl.len().alias("g"), pl.col("game_poss").sum().alias("poss")).rename({"team": "def"})
    by_pos = per_game.group_by(["def", "pos"]).agg([pl.col(s).sum() for s in STATS]).join(games, on="def", how="left")
    tot = per_game.group_by("def").agg([pl.col(s).sum() for s in STATS]).join(games, on="def", how="left")
    del poss
    out = {}
    for s in STATS:
        rows = {}
        for r in tot.iter_rows(named=True):
            if not r["g"]:
                continue
            rows[r["def"]] = {"team": r["def"], "badge": teams.badge(r["def"]), "g": r["g"],
                              "all": round(r[s] / r["g"], 1), "all100": round(100 * r[s] / r["poss"], 1) if r["poss"] else None}
        for r in by_pos.iter_rows(named=True):
            if r["def"] in rows and r["g"]:
                rows[r["def"]][r["pos"]] = round(r[s] / r["g"], 1)
        lst = list(rows.values())
        league = {k: (round(sum(x.get(k) or 0 for x in lst) / len(lst), 1) if lst else None) for k in ("all", "G", "F", "C")}
        for k in ("all", "G", "F", "C"):
            ranked = sorted([x for x in lst if x.get(k) is not None], key=lambda x: -x[k])
            for i, x in enumerate(ranked, 1):
                x[f"{k}_rank"] = i
                x[f"{k}_vs"] = round(100 * (x[k] / league[k] - 1)) if league[k] else 0
        lst.sort(key=lambda x: x.get("all_rank", 99))
        out[s] = {"rows": lst, "league": league, "n": len(lst)}
    return out


def build(box: pl.DataFrame) -> dict:
    cur = dataio.CURRENT_SEASON
    st = standings(box, cur)
    al = allowed(box, cur)
    prev = allowed(box, cur - 1)
    games = min((r["g"] for r in st), default=0)
    return {"season": cur, "standings": st, "allowed": al, "allowed_prev": prev, "min_games": games,
            "max_games": max((r["g"] for r in st), default=0),
            "stats": [(s, model.STAT_LABEL.get(s, s)) for s in STATS], "pos": POS}


def matchup(rk: dict, opp: str, pos: str, stat: str) -> dict | None:
    """What `opp` allows to `pos` for `stat` this season, with rank and % vs league."""
    a = (rk.get("allowed") or {}).get(stat)
    if not a or not opp or pos not in ("G", "F", "C"):
        return None
    row = next((r for r in a["rows"] if r["team"] == opp), None)
    if not row or row.get(pos) is None:
        return None
    return {"opp": opp, "pos": pos, "pos_name": dict(POS)[pos], "stat": stat, "val": row[pos],
            "rank": row.get(f"{pos}_rank"), "n": a["n"], "vs": row.get(f"{pos}_vs"), "g": row["g"]}
