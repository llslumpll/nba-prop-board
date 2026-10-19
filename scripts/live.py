"""
Live slate: who plays next, their projections, the market lines, and the
pick for every player/stat that has a line.

Slate = every game on the next US-Eastern date that still has a game to
tip (games on that date that already tipped stay listed, locked).
Roster = ESPN's current roster (rosters_client), else each player's last
box-score team. Injury report: Out/Doubtful players are removed and their
minutes and production are handed to teammates by the model.

The model and constants are the ones the backtest tuned (data/backtest.json).
Rookies with no NBA box scores get no projection: listed, never guessed.
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import polars as pl

import dataio
import grade
import markets
import model
import teams

ET = ZoneInfo("America/New_York")
DATA = Path(__file__).parent.parent / "data"
VAR_PATH = DATA / "player_var.json"
PICK_STATS = ("pts", "reb", "ast", "fg3m", "pra", "pr", "pa", "ra")

# Game picks (moneyline / spread / total) come from team scores built by
# summing player projections. Early in a season those sums run high (players
# who changed teams still project like their old role), so no game pick is
# made until BOTH teams have this many regular-season games in the box scores.
GAME_PICKS_MIN_GAMES = 10


def team_games_played(box: pl.DataFrame) -> dict:
    if not box.height:
        return {}
    cur = box.filter((pl.col("season") == dataio.CURRENT_SEASON) & (pl.col("season_type") == model.REGULAR))
    if not cur.height:
        return {}
    return dict(cur.group_by("team").agg(pl.col("game_id").n_unique()).iter_rows())


def game_picks_open(g: dict, played: dict) -> bool:
    return min(played.get(g["away"], 0), played.get(g["home"], 0)) >= GAME_PICKS_MIN_GAMES


def load_fit() -> dict:
    try:
        r = json.loads((DATA / "backtest.json").read_text())
    except Exception:  # noqa: BLE001
        r = {}
    prm = dict(r.get("params") or {})
    if "season_w" in prm:
        prm["season_w"] = tuple(prm["season_w"])
    fields = model.Params.__dataclass_fields__
    p = model.Params(**{k: v for k, v in prm.items() if k in fields})
    vf = r.get("variance_fit") or {s: {"phi": 1.5, "kappa": 0.02} for s in model.STATS}
    tf = r.get("team_fit_2026") or {"scale": 1.0, "margin_sd": 20.0, "total_sd": 27.0, "margin_bias": 0.0}
    return {"params": p, "var": vf, "team": tf, "backtest": r}


def tier_for(row: dict) -> str:
    n = row.get("c_n") or 0
    if n >= 15:
        return "15+ games this season"
    if n >= 5:
        return "5-14 games this season"
    if n >= 1:
        return "1-4 games this season"
    if (row.get("g1") or 0) + (row.get("g2") or 0) > 0:
        return "Prior seasons only"
    return "Limited history"


# ---------------------------------------------------------------------------
# Slate
# ---------------------------------------------------------------------------

def slate(now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    s = dataio.load_schedule()
    if not s.height:
        return {"date": None, "games": []}
    s = s.filter(pl.col("home").is_not_null() & pl.col("away").is_not_null() & pl.col("tip_utc").is_not_null())
    fut = s.filter((pl.col("tip_utc") > now) & ~pl.col("completed"))
    if not fut.height:
        return {"date": None, "games": []}
    first = fut.sort("tip_utc")["tip_utc"][0].astimezone(ET).date()
    games = []
    for r in s.sort("tip_utc").iter_rows(named=True):
        d = r["tip_utc"].astimezone(ET).date()
        if d != first:
            continue
        games.append({
            "game_id": r["game_id"], "away": r["away"], "home": r["home"], "date": d.isoformat(),
            "tip_utc": r["tip_utc"], "tip_et": r["tip_utc"].astimezone(ET).strftime("%-I:%M %p ET"),
            "started": now >= r["tip_utc"], "completed": r["completed"], "note": r.get("notes_headline"),
            "away_badge": teams.badge(r["away"]), "home_badge": teams.badge(r["home"]),
        })
    return {"date": first.isoformat(), "date_label": first.strftime("%a %b %-d"), "games": games}


def roster_rows(box: pl.DataFrame, rosters: dict) -> tuple[list[tuple], str]:
    """[(player_id, player, team)], source label."""
    if rosters.get("players"):
        return [(p["player_id"], p["player"], p["team"]) for p in rosters["players"] if p.get("team")], "ESPN roster"
    last = box.filter(pl.col("played")).sort("date").group_by("player_id").agg(
        pl.col("player").last(), pl.col("team").last(), pl.col("date").last())
    cutoff = last["date"].max() - timedelta(days=200) if last.height else None
    if cutoff is not None:
        last = last.filter(pl.col("date") >= cutoff)
    return [(r["player_id"], r["player"], r["team"]) for r in last.iter_rows(named=True)], "team from last box score"


def targets(sl: dict, roster: list[tuple], injuries: dict) -> pl.DataFrame:
    inj = {p["player"]: p for p in injuries.get("players", []) if p.get("player")}
    rows = []
    teams_in = {}
    for g in sl["games"]:
        teams_in[g["away"]] = (g, False)
        teams_in[g["home"]] = (g, True)
    for pid, name, team in roster:
        if team not in teams_in:
            continue
        g, home = teams_in[team]
        status = inj.get(name)
        rows.append({"player_id": int(pid), "player": name, "team": team, "opp": g["home"] if not home else g["away"],
                     "game_id": int(g["game_id"]), "date": datetime.fromisoformat(g["date"]).date(),
                     "season": dataio.CURRENT_SEASON, "home": home,
                     "present": not (status and status.get("class") == "out"),
                     "inj_status": status.get("status") if status else None,
                     "inj_note": status.get("comment") if status else None})
    if not rows:
        return pl.DataFrame()
    return pl.DataFrame(rows)


# ---------------------------------------------------------------------------
# Player variance multipliers (heavy build only)
# ---------------------------------------------------------------------------

def refresh_player_var(box: pl.DataFrame, fit: dict) -> int:
    """Each player's own miss size vs the model, from his last two seasons
    of walk-forward projections, shrunk toward 1 (var_k games)."""
    import backtest
    p = fit["params"]
    seasons = sorted(box["season"].unique().to_list())[-2:]
    frames = []
    for s in seasons:
        t = backtest.targets_for(box, s)
        if t.height:
            frames.append(backtest.attach_actuals(model.project(box, t, p), box))
    if not frames:
        return 0
    df = pl.concat(frames, how="diagonal_relaxed")
    out: dict = {}
    for s in model.STATS:
        vf = fit["var"][s]
        mu = pl.col(f"proj_{s}")
        var = (vf["phi"] * mu + vf["kappa"] * mu * mu).clip(0.25, None)
        agg = df.filter(mu.is_not_null() & (pl.col("proj_min") >= 10)).with_columns(
            z2=(pl.col(s) - mu).pow(2) / var).group_by("player_id").agg(pl.col("z2").sum(), pl.len().alias("n"))
        for pid, z2, n in agg.iter_rows():
            out.setdefault(str(pid), {})[s] = round(min(1.8, max(0.6, (z2 + p.var_k) / (n + p.var_k))), 3)
    VAR_PATH.write_text(json.dumps(out, sort_keys=True))
    return len(out)


def _load_var():
    try:
        return json.loads(VAR_PATH.read_text())
    except Exception:  # noqa: BLE001
        return {}


# ---------------------------------------------------------------------------
# Projections + picks
# ---------------------------------------------------------------------------

def _pct(x):
    return f"{'+' if x >= 0 else '−'}{abs(x) * 100:.0f}%"


def why_text(r: dict, s: str, cal_bias: float) -> str:
    label = model.STAT_LABEL[s]
    parts = []
    mins = f"{r['proj_min']:.1f} min (recent {r['min_base']:.1f}"
    if abs(r["min_extra"]) >= 0.3:
        mins += f", {'+' if r['min_extra'] > 0 else '−'}{abs(r['min_extra']):.1f} team-minutes adjustment"
        if r.get("out_players"):
            mins += f" with {', '.join(r['out_players'][:3])} out"
    if r["min_b2b"] < -0.2:
        mins += f", −{abs(r['min_b2b']):.1f} back-to-back"
    if r["min_blowout"] < -0.2:
        mins += f", −{abs(r['min_blowout']):.1f} blowout risk ({r['margin_source']} margin {abs(r['exp_margin']):.0f})"
    parts.append(mins + ")")
    w = r[f"w_cur_{s}"]
    if r["c_n"]:
        parts.append(f"× {r[f'rate_{s}']:.3f} {label}/min ({w * 100:.0f}% this season's {r[f'cur_rate_{s}']:.3f} over "
                     f"{r['c_n']} games, {100 - w * 100:.0f}% prior {r[f'prior_rate_{s}']:.3f})")
    else:
        parts.append(f"× {r[f'rate_{s}']:.3f} {label}/min (prior seasons, no games this season yet)")
    ctx = []
    for name, f in (("pace vs " + r["opp"], r["f_pace"]), ("home" if r["home"] else "road", r["f_home"]),
                    ("rest", r["f_rest"]), ("opponent defense", r[f"f_def_{s}"]), ("usage", r[f"f_use_{s}"]),
                    ("shared production on " + r["team"], r.get(f"f_team_{s}") or 1.0)):
        if abs(f - 1) >= 0.005:
            ctx.append(f"{name} {_pct(f - 1)}")
    if ctx:
        parts.append("context: " + ", ".join(ctx))
    if cal_bias:
        parts.append(f"calibration {cal_bias:+.1f}")
    if r.get("inj_status"):
        parts.append(f"listed {r['inj_status']}")
    return "; ".join(parts) + "."


def run(box, sl, roster, injuries, line_book: dict, fit: dict, spreads: dict | None = None) -> dict:
    """Projects the slate. Returns players (per game), picks (per stat) and
    team projections. line_book: {(player, stat, game_id): {source: rec}}."""
    if not sl["games"]:
        return {"players": [], "picks": {s: [] for s in PICK_STATS}, "teams": {}, "no_history": []}
    t = targets(sl, roster, injuries)
    if not t.height:
        return {"players": [], "picks": {s: [] for s in PICK_STATS}, "teams": {}, "no_history": []}
    team_dates = dataio.load_schedule().select(pl.col("home").alias("team"), "date").vstack(
        dataio.load_schedule().select(pl.col("away").alias("team"), "date")).drop_nulls()
    proj = model.project(box, t, fit["params"], team_dates=team_dates, spreads=spreads)
    present_ids = set(proj["player_id"].to_list())
    no_hist = [r for r in t.filter(pl.col("present")).iter_rows(named=True) if r["player_id"] not in present_ids]
    cal = grade.load_calibration()
    vmult = _load_var()
    games = {g["game_id"]: g for g in sl["games"]}

    players, picks = [], {s: [] for s in PICK_STATS}
    for r in proj.sort("proj_pts", descending=True).iter_rows(named=True):
        g = games[r["game_id"]]
        tier = tier_for(r)
        row = {"player": r["player"], "player_id": r["player_id"], "team": r["team"], "opp": r["opp"],
               "home": r["home"], "game_id": r["game_id"], "badge": teams.badge(r["team"]),
               "proj_min": round(r["proj_min"], 1), "tier": tier, "inj_status": r.get("inj_status"),
               "inj_note": r.get("inj_note"), "stats": {}}
        for s in model.STATS:
            tc = ((cal.get("bias") or {}).get(s) or {}).get(tier) or {}
            bias = tc.get("bias", 0.0) if tc.get("status") == "active" else 0.0
            mu = max(0.0, r[f"proj_{s}"] + bias)
            vf = fit["var"][s]
            var = (vf["phi"] * mu + vf["kappa"] * mu * mu) * (vmult.get(str(r["player_id"]), {}).get(s, 1.0))
            st = {"proj": round(mu, 1), "var": round(var, 3), "why": why_text(r, s, bias), "lines": {}, "pick": None}
            book = line_book.get((r["player"], s, r["game_id"])) or {}
            shrink = (((cal.get("chance_shrink") or {}).get(s) or {}).get("shrink", 1.0)
                      if (((cal.get("chance_shrink") or {}).get(s) or {}).get("status") == "active") else 1.0)
            for src, ln in book.items():
                po = model.prob_over(s, mu, var, ln["line"])
                if po is None:
                    continue
                po = 0.5 + (po - 0.5) * shrink
                side = "OVER" if po >= 0.5 else "UNDER"
                chance = po if side == "OVER" else 1 - po
                be = ln.get("be_over") if side == "OVER" else ln.get("be_under")
                mkt = ln.get("market_over")
                mkt_side = (mkt if side == "OVER" else 1 - mkt) if mkt is not None else None
                st["lines"][src] = {**ln, "p_over": round(po, 4), "side": side, "chance": round(chance, 4),
                                    "break_even": be, "value": round(chance - be, 4) if be is not None else None,
                                    "market_side": round(mkt_side, 4) if mkt_side is not None else None}
            psrc, _ = markets.primary(st["lines"])
            if psrc:
                pk = st["lines"][psrc]
                st["pick"] = {"source": psrc, **{k: pk[k] for k in ("line", "side", "chance", "break_even", "value", "market_side")}}
                if s in PICK_STATS:
                    picks[s].append({**{k: row[k] for k in ("player", "player_id", "team", "opp", "home", "game_id",
                                                            "badge", "tier", "proj_min", "inj_status")},
                                     "stat": s, "proj": st["proj"], "var": st["var"], "why": st["why"],
                                     "lines": st["lines"], **st["pick"],
                                     "tip_utc": g["tip_utc"], "started": g["started"], "date": g["date"]})
            row["stats"][s] = st
        players.append(row)

    # team projections from summed player points
    tf = fit["team"]
    tp = proj.group_by(["game_id", "team"]).agg(pl.col("proj_pts").sum().alias("pts"))
    team_pts = {(gid, tm): v * tf["scale"] for gid, tm, v in tp.iter_rows()}
    team_out = {}
    for gid, g in games.items():
        hp, ap = team_pts.get((gid, g["home"])), team_pts.get((gid, g["away"]))
        if hp is None or ap is None:
            continue
        margin = hp - ap - tf.get("margin_bias", 0.0)
        team_out[gid] = {"home_pts": round(hp, 1), "away_pts": round(ap, 1), "margin": round(margin, 1),
                         "total": round(hp + ap, 1),
                         "home_win": round(model._phi(margin / tf["margin_sd"]), 4),
                         "margin_sd": tf["margin_sd"], "total_sd": tf["total_sd"]}
    return {"players": players, "picks": picks, "teams": team_out,
            "no_history": [{"player": r["player"], "team": r["team"]} for r in no_hist]}


def team_market_chances(tp: dict, home_spread: float | None, total: float | None) -> dict:
    out = {}
    if home_spread is not None:
        # home covers when margin + home_spread > 0
        p_home = model._phi((tp["margin"] + home_spread) / tp["margin_sd"])
        out["spread"] = {"p_home": round(p_home, 4)}
    if total is not None:
        out["total"] = {"p_over": round(1 - model._phi((total - tp["total"]) / tp["total_sd"]), 4)}
    return out
