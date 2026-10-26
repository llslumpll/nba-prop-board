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


# ---- turning a projection into a chance ------------------------------------
# BLEND_W: weight on our projection when it's blended with the line before the
# chance is computed (the rest goes to the line). On the first graded night
# (Oct 20, 324 picks) a ~1/3 model, ~2/3 line blend had a lower average miss
# than either alone, and the raw chances were worse than a coin flip (Brier
# 0.262 vs 0.250); blended, 0.249. grade.calibrate() re-fits this weight once
# BLEND_MIN_N graded picks exist.
# EARLY_VAR: a player's range is widened by up to this factor until he has
# EARLY_GAMES games this season (his role is the big unknown early on).
# CHANCE_CAP: no prop is shown above this chance.
BLEND_W = 0.35
EARLY_VAR = 1.3
EARLY_GAMES = 10
CHANCE_CAP = 0.80
# ROLE_GAP: when our projection is more than this share away from the line
# for a player with < EARLY_GAMES games this season, the gap is most likely a
# role or minutes change the market knows about (Oct 20: picks 50%+ off the
# line hit 37.5%). Those picks get a smaller model weight and are kept out of
# Best 5 / Best Value.
ROLE_GAP = 0.5


def implied_mean(s: str, line: float, p_over: float, vscale) -> float:
    """Mean m at which P(stat > line) = p_over under our distribution shape
    (variance vscale(m)). For points this is ~the line; for low counts like a
    0.5 threes line it's ~0.7-0.9, not 0.5."""
    lo, hi = 1e-3, max(2.0, line * 3 + 5)
    for _ in range(50):
        mid = (lo + hi) / 2
        po = model.prob_over(s, mid, vscale(mid), line)
        if po is None:
            return line
        lo, hi = (mid, hi) if po < p_over else (lo, mid)
    return (lo + hi) / 2


def _early_var_mult(r: dict) -> float:
    n = int(r.get("c_n") or 0)
    return 1 + (EARLY_VAR - 1) * max(0.0, 1 - n / EARLY_GAMES)


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
    if "last_game_w" not in prm:
        # Oct 26 minutes update: trust this season sooner. Tested on games it wasn't fitted on:
        # minutes miss 4.75 -> 4.31 (first 3 weeks of 2025-26), 4.97 -> 4.62 (this season so far),
        # 4.18 -> 4.13 mid-season; points, rebounds and PRA misses also down. A retune that saves
        # its own values for these keys takes over from here.
        prm.update(min_prior_k=1.0, last_game_w=0.25)
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

LIVE_HOLD_H = 5

# ---- recommended units: how much to stake on a pick, from its edge over the price
# edge = our chance - the price's break-even. Steps chosen so 1U needs a 6-point edge;
# below break-even the site says PASS (0U). UNIT_TRUST (0.5-1.0) is set every build by
# grade.unit_trust() from how the sized picks have actually done: when they lose money,
# every edge is shrunk before sizing, so stakes come down until results earn them back.
UNIT_STEPS = ((0.13, 2.0), (0.09, 1.5), (0.06, 1.0), (0.04, 0.75), (0.02, 0.5), (0.0, 0.25))
UNIT_NAMES = {0.0: "PASS", 0.25: "LOW", 0.5: "MEDIUM", 0.75: "LEAN", 1.0: "CONFIDENT", 1.5: "HIGH", 2.0: "EXTREME"}
UNIT_TRUST = 1.0
GAME_SKIP: set = set()  # active game-pick skip patterns, set by build.py
DEFAULT_BE = 0.5238  # -110 when a source has no price


def unit_rec(chance, be, role_gap=False, inj=None, trust=None) -> float:
    if chance is None:
        return 0.0
    be = be if be is not None else DEFAULT_BE
    if chance < be:
        return 0.0
    edge = (chance - be) * (UNIT_TRUST if trust is None else trust)
    u = next(u for t, u in UNIT_STEPS if edge >= t)
    if role_gap:
        u = min(u, 0.25)  # projection far from the line: likely a role change the market knows about
    if inj:
        u = min(u, 0.5)   # questionable / day-to-day
    return u



def slate(now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    s = dataio.load_schedule()
    if not s.height:
        return {"date": None, "games": []}
    s = s.filter(pl.col("home").is_not_null() & pl.col("away").is_not_null() & pl.col("tip_utc").is_not_null())
    # Stay on tonight's date until its last game is final (ESPN marks finals within
    # minutes, see espn_finals.py); a game nobody has marked final drops off 5h after tip.
    fut = s.filter((pl.col("tip_utc") > now - timedelta(hours=LIVE_HOLD_H)) & ~pl.col("completed"))
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


def run(box, sl, roster, injuries, line_book: dict, fit: dict, spreads: dict | None = None,
        pat: dict | None = None, open_lines: dict | None = None) -> dict:
    """Projects the slate. Returns players (per game), picks (per stat) and
    team projections. line_book: {(player, stat, game_id): {source: rec}}.
    pat: active pattern corrections (patterns.active()); open_lines:
    {(player, stat, game_id): (source, opening line)} of recorded picks."""
    pat = pat or {"skip": set(), "over": {}, "move": {}}
    open_lines = open_lines or {}
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
    blend_w = (cal.get("blend") or {}).get("w", BLEND_W) if (cal.get("blend") or {}).get("status") == "active" else BLEND_W
    games = {g["game_id"]: g for g in sl["games"]}

    players, picks = [], {s: [] for s in PICK_STATS}
    for r in proj.sort("proj_pts", descending=True).iter_rows(named=True):
        g = games[r["game_id"]]
        tier = tier_for(r)
        row = {"player": r["player"], "player_id": r["player_id"], "team": r["team"], "opp": r["opp"],
               "home": r["home"], "game_id": r["game_id"], "badge": teams.badge(r["team"]),
               "proj_min": round(r["proj_min"], 1), "tier": tier, "inj_status": r.get("inj_status"),
               "inj_note": r.get("inj_note"), "stats": {},
               "factors": {"min_recent": round(r["min_base"], 1), "min_adj": round(r["min_extra"] + r["min_b2b"] + r["min_blowout"], 1),
                           "pace": round(r["f_pace"], 3), "home": round(r["f_home"], 3), "rest": round(r["f_rest"], 3),
                           "rest_days": r.get("rest"), "games_this_season": int(r.get("c_n") or 0),
                           "team": {s: round(r.get(f"f_team_{s}") or 1.0, 3) for s in model.STATS}}}
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
            vm_p = (vmult.get(str(r["player_id"]), {}).get(s, 1.0)) * _early_var_mult(r)
            vscale = (lambda m, vf=vf, vm_p=vm_p: max(1e-6, (vf["phi"] * m + vf["kappa"] * m * m) * vm_p))
            for src, ln in book.items():
                # chance comes from our projection blended with the line itself (markets are sharp;
                # see BLEND_W), with a wider range early in the season, capped at CHANCE_CAP
                # the average the market's own line implies: the mean at which our distribution gives the
                # market's chance of going over (no-vig price where the source has one, else 50/50)
                m_mkt = implied_mean(s, ln["line"], ln.get("market_over") if ln.get("market_over") is not None else 0.5, vscale)
                gap = abs(mu - m_mkt) / max(m_mkt, 1.0)
                role_gap = gap > ROLE_GAP and int(r.get("c_n") or 0) < EARLY_GAMES
                w_eff = blend_w * (ROLE_GAP / gap) if role_gap else blend_w
                mu_b = m_mkt + w_eff * (mu - m_mkt)
                po = model.prob_over(s, mu_b, vscale(mu_b), ln["line"])
                if po is None:
                    continue
                po = 0.5 + (po - 0.5) * shrink
                adj = []
                if pat["over"].get((s, src)):
                    po += pat["over"][(s, src)]  # learned: this stat's lines on this source run high / low
                    adj.append(f"OVER chance {pat['over'][(s, src)] * 100:+.1f} pts (pattern)")
                po = min(CHANCE_CAP, max(1 - CHANCE_CAP, po))
                side = "OVER" if po >= 0.5 else "UNDER"
                chance = po if side == "OVER" else 1 - po
                op = open_lines.get((r["player"], s, r["game_id"]))
                move = round(ln["line"] - op[1], 1) if op and op[0] == src and op[1] is not None else None
                mdir = None if not move else ("agree" if (move > 0) == (side == "OVER") else "against")
                if mdir and pat["move"].get(mdir):
                    chance = min(CHANCE_CAP, max(1 - CHANCE_CAP, chance + pat["move"][mdir]))
                    adj.append(f"{pat['move'][mdir] * 100:+.1f} pts: pick {'follows' if mdir == 'agree' else 'goes against'} the line's move (pattern)")
                    if chance < 0.5:
                        side, chance = ("UNDER" if side == "OVER" else "OVER"), 1 - chance
                        mdir = "agree" if mdir == "against" else "against"
                    po = chance if side == "OVER" else 1 - chance
                skip = next((k for k in (f"{s}|{src}|{side}", f"{s}|{src}") if k in pat["skip"]), None)
                be = ln.get("be_over") if side == "OVER" else ln.get("be_under")
                mkt = ln.get("market_over")
                mkt_side = (mkt if side == "OVER" else 1 - mkt) if mkt is not None else None
                st["lines"][src] = {**ln, "units": 0.0 if skip else unit_rec(chance, be, role_gap, r.get("inj_status")),
                                    "units_raw": 0.0 if skip else unit_rec(chance, be, role_gap, r.get("inj_status"), trust=1.0),
                                    "skip": skip, "move": move, "move_dir": mdir, "adj": adj,
                                    "p_over": round(po, 4), "side": side, "chance": round(chance, 4), "blend_mean": round(mu_b, 2), "market_mean": round(m_mkt, 2), "role_gap": role_gap,
                                    "break_even": be, "value": round(chance - be, 4) if be is not None else None,
                                    "market_side": round(mkt_side, 4) if mkt_side is not None else None}
            psrc, _ = markets.primary(st["lines"])
            if psrc:
                pk = st["lines"][psrc]
                st["pick"] = {"source": psrc, "blend_w": blend_w, **{k: pk[k] for k in ("line", "side", "chance", "break_even", "value", "market_side", "blend_mean", "role_gap", "units", "units_raw",
                                                                         "skip", "move", "move_dir", "adj")}}
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


# ---- game picks (moneyline / spread / total) ---------------------------------
# Same idea as player props: start from what the market implies (FanDuel's
# spread for the margin, its total for the points), move GAME_BLEND_W of the
# way toward our own team projection (summed player projections), cap the
# chance. Team sums run high early in a season, so the weight is lower than
# for props. Replaces the old "wait 10 games" gate.
GAME_BLEND_W = 0.25
GAME_CHANCE_CAP = 0.75   # spreads and totals; moneylines stay within 5-95%
# Shifts are converted to chances on the market's own scale (NBA final
# margins vary about 12.5 points around the spread and totals about 18 around
# the total), so a pick only moves away from the market where we disagree.
MARKET_MARGIN_SD = 12.5
MARKET_TOTAL_SD = 18.0


def american_be(price) -> float | None:
    if price is None:
        return None
    price = float(price)
    return round(100 / (price + 100), 4) if price > 0 else round(-price / (-price + 100), 4)


def _fmt_pt(x: float) -> str:
    return f"{x:+g}" if x else "PK"


def agreement(pairs: list[tuple[float, float]]) -> float:
    """Correlation between our numbers and the market's over every game so far
    (pooled across nights); 0 until there are 8 games. Our weight in game
    picks is GAME_BLEND_W times this, so our team numbers only count as far
    as they track the market's own ordering of games."""
    if len(pairs) < 8:
        return 0.0
    xs, ys = [p[0] for p in pairs], [p[1] for p in pairs]
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    sxy = sum((x - mx) * (y - my) for x, y in pairs)
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    return round(max(0.0, sxy / (sxx * syy) ** 0.5), 3) if sxx > 0 and syy > 0 else 0.0


def game_picks(g: dict, tp: dict, kal_ml: dict | None = None, total_offset: float = 0.0,
               agree_m: float = 1.0, agree_t: float = 1.0) -> list[dict]:
    from statistics import NormalDist
    fd = g.get("fanduel") or {}
    ml, sp, tot = fd.get("moneyline"), fd.get("spread"), fd.get("total")
    msd, tsd = tp["margin_sd"], tp["total_sd"]
    k_home = (kal_ml or {}).get("home_prob")
    msd, tsd = MARKET_MARGIN_SD, MARKET_TOTAL_SD
    inv = lambda p: NormalDist().inv_cdf(min(0.99, max(0.01, p)))
    if sp:
        mm, msrc = -float(sp["home_point"]), f"FanDuel spread {g['home']} {_fmt_pt(sp['home_point'])}"
    elif ml:
        mm, msrc = msd * inv(ml["home_prob"] / 100), "FanDuel moneyline"
    elif k_home:
        mm, msrc = msd * inv(k_home / 100), "Kalshi price"
    else:
        return []
    m = tp["margin"]
    wm, wt = GAME_BLEND_W * agree_m, GAME_BLEND_W * agree_t
    mb = mm + wm * (m - mm)
    cap = lambda p: min(GAME_CHANCE_CAP, max(1 - GAME_CHANCE_CAP, p))
    base_why = (f"Our score {g['away']} {tp['away_pts']:.0f}, {g['home']} {tp['home_pts']:.0f} (summed player projections), "
                f"margin {m:+.1f} for {g['home']}; market margin {mm:+.1f} ({msrc}). So far our margins track the market's "
                f"with a correlation of {agree_m:.2f}, so ours gets {wm * 100:.0f}% weight: {g['home']} by {mb:+.1f}.")
    out = []
    if ml or k_home:
        p_mkt_home = (ml["home_prob"] if ml else k_home) / 100
        ph = min(0.95, max(0.05, model._phi(inv(p_mkt_home) + (mb - mm) / msd)))
        home = ph >= 0.5
        team = g["home"] if home else g["away"]
        if ml:
            price = ml["home_price"] if home else ml["away_price"]
            be, src, pstr = american_be(price), "fanduel", (ml["home_str"] if home else ml["away_str"])
            mside = (ml["home_prob"] if home else ml["away_prob"]) / 100
        else:
            be, src, pstr = (kal_ml.get("home_ask") if home else kal_ml.get("away_ask")), "kalshi", None
            mside = (k_home if home else (kal_ml.get("away_prob") or 100 - k_home)) / 100
        ch = ph if home else 1 - ph
        out.append({"market": "moneyline", "pick": team, "line": None, "pick_label": f"{team} ML", "price_str": pstr,
                    "chance": round(ch, 4), "break_even": be, "value": round(ch - be, 4) if be else None,
                    "market_side": round(mside, 4), "source": src,
                    "why": base_why + f" {team} wins {ch * 100:.0f}% (market {mside * 100:.0f}%)."})
    if sp:
        hp = float(sp["home_point"])
        p_home = cap(model._phi((mb - mm) / msd))
        home = p_home >= 0.5
        team = g["home"] if home else g["away"]
        pt = hp if home else -hp
        ch = p_home if home else 1 - p_home
        be = american_be(sp.get("home_price") if home else sp.get("away_price"))
        out.append({"market": "spread", "pick": team, "line": hp, "pick_label": f"{team} {_fmt_pt(pt)}",
                    "price_str": sp["home_price_str"] if home else sp["away_price_str"],
                    "chance": round(ch, 4), "break_even": be, "value": round(ch - be, 4) if be else None, "source": "fanduel",
                    "why": base_why + f" {team} {_fmt_pt(pt)} covers {ch * 100:.0f}%."})
    if tot:
        mt = float(tot["point"])
        our_t = tp["total"] - total_offset  # remove the slate-wide gap: only how this game differs counts
        tb = mt + wt * (our_t - mt)
        po = cap(1 - model._phi((mt - tb) / tsd))
        over = po >= 0.5
        ch = po if over else 1 - po
        be = american_be(tot.get("over_price") if over else tot.get("under_price"))
        out.append({"market": "total", "pick": "OVER" if over else "UNDER", "line": mt,
                    "pick_label": f"{'OVER' if over else 'UNDER'} {mt:g}", "price_str": tot["over_str"] if over else tot["under_str"],
                    "chance": round(ch, 4), "break_even": be, "value": round(ch - be, 4) if be else None, "source": "fanduel",
                    "our_total": round(our_t, 1), "blend_total": round(tb, 1),
                    "why": (f"Our total {tp['total']:.0f} ({g['away']} {tp['away_pts']:.0f} + {g['home']} {tp['home_pts']:.0f})"
                            + (f", {abs(total_offset):.1f} {'above' if total_offset > 0 else 'below'} the market on average tonight, so {our_t:.0f} after removing that" if abs(total_offset) >= 0.5 else "")
                            + "; "
                            f"FanDuel {mt:g}. Our totals track the market's with a correlation of {agree_t:.2f} so far, "
                            f"so ours gets {wt * 100:.0f}% weight: {tb:.1f}. {'Over' if over else 'Under'} {mt:g} hits {ch * 100:.0f}%.")})
    for p in out:
        p.update(our_margin=round(m, 1), market_margin=round(mm, 1), blend_margin=round(mb, 1),
                 blend_w=round(wt if p["market"] == "total" else wm, 4), agree=agree_t if p["market"] == "total" else agree_m)
        p.update(units=unit_rec(p["chance"], p["break_even"]), units_raw=unit_rec(p["chance"], p["break_even"], trust=1.0))
        import patterns as _patterns  # local: patterns imports live
        sk = next((k for k in reversed(_patterns.skip_keys(p)) if k in GAME_SKIP), None)
        if sk:
            p.update(units=0.0, units_raw=0.0, skip=sk)
    return out


def team_market_chances(tp: dict, home_spread: float | None, total: float | None) -> dict:
    out = {}
    if home_spread is not None:
        # home covers when margin + home_spread > 0
        p_home = model._phi((tp["margin"] + home_spread) / tp["margin_sd"])
        out["spread"] = {"p_home": round(p_home, 4)}
    if total is not None:
        out["total"] = {"p_over": round(1 - model._phi((total - tp["total"]) / tp["total_sd"]), 4)}
    return out
