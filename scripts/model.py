"""
NBA projection model: minutes first, then per-minute rates, then dampened
context, then a count distribution.

The same function serves the walk-forward backtest and the live site. Every
feature for a target row is built ONLY from games strictly before that row's
date (as-of joins with allow_exact_matches=False), so a backtest row never
sees its own result and a live row is projected exactly the same way.

  minutes   = recency-weighted minutes (EWM, half-life MIN_HALF_LIFE games)
              + share of minutes freed by rotation teammates who are out
              - back-to-back cut - blowout cut (starters, big expected margin)
  rate      = (n * rate_this_season + k * rate_prior) / (n + k)
              n = games this season so far, k per stat (tuned by backtest)
              rate_prior = seasons weighted 1.0 / 0.5 (most recent first),
                           itself shrunk to the position rate with K_POS games
  context   = opponent pace, opponent defense vs this stat at this position,
              home/away, rest, usage freed by teammates out.
              Each one dampened and capped at +/-CAP.
  projected = minutes * rate * context
  spread    = negative binomial (reb, ast, 3PM) or normal (points, combos),
              variance phi*mu + kappa*mu^2 fit on history, scaled per player
              by his own past miss size (shrunk toward 1).

Regular-season games only feed the rates. Playoff/play-in rows are ignored
for now (different minutes and opponents); they are projected separately
later in the season.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from datetime import timedelta

import polars as pl

REGULAR = 2

STATS = {
    "pts": ("pts",), "reb": ("reb",), "ast": ("ast",), "fg3m": ("fg3m",),
    "pra": ("pts", "reb", "ast"), "pr": ("pts", "reb"), "pa": ("pts", "ast"), "ra": ("reb", "ast"),
}
STAT_LABEL = {
    "pts": "points", "reb": "rebounds", "ast": "assists", "fg3m": "3-pointers made",
    "pra": "pts+reb+ast", "pr": "pts+reb", "pa": "pts+ast", "ra": "reb+ast",
}
COUNT_STATS = ("reb", "ast", "fg3m")  # negative binomial; the rest use a normal approximation


@dataclass
class Params:
    min_half_life: float = 8.0
    k: dict = field(default_factory=lambda: {"pts": 10, "reb": 8, "ast": 8, "fg3m": 15,
                                             "pra": 10, "pr": 10, "pa": 10, "ra": 8})
    k_pos: float = 15.0
    season_w: tuple = (1.0, 0.5)
    cap: float = 0.06
    a_pace: float = 0.5
    a_def: float = 0.5
    def_k: float = 20.0
    home: float = 0.01
    b2b_rate: float = -0.02
    rest3_rate: float = 0.01
    b2b_min: float = -0.04
    b2b_min_threshold: float = 28.0
    redistribute: float = 0.5
    usage_b: float = 0.3
    usage_cap: float = 0.10
    blowout_c: float = 0.15
    blowout_threshold: float = 8.0
    blowout_cap: float = 3.0
    rotation_min: float = 12.0
    absent_window_days: int = 10
    min_cap: float = 44.0
    rotation_cap: float = 250.0   # team minutes covered before a player counts as fringe
    fringe_share: float = 0.35    # share of recent minutes a fringe player is projected for
    team_min_cap: float = 241.0   # 240 + the average overtime
    cap_mode: str = "waterfall"   # "waterfall" (cut deepest first) or "proportional"
    min_prior_k: float = 4.0      # games before last season's minutes stop mattering
    last_game_w: float = 0.0      # extra weight on this season's most recent game's minutes (role changes)
    team_share_alpha: float = 0.5  # how far toward the team norm (0 = off, 1 = all the way)
    team_share_cap: float = 0.12
    min_mode: str = "normalize"   # "normalize" (team total ~241) or "freed" (add freed minutes)
    var_k: float = 20.0
    use_pace: bool = True
    use_def: bool = True
    use_home_rest: bool = True
    use_out: bool = True
    use_blowout: bool = True


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------

def _with_combos(df: pl.DataFrame) -> pl.DataFrame:
    cols = []
    for s, parts in STATS.items():
        if len(parts) > 1:
            expr = pl.col(parts[0])
            for p in parts[1:]:
                expr = expr + pl.col(p)
            cols.append(expr.alias(s))
    return df.with_columns(cols)


def _played(box: pl.DataFrame) -> pl.DataFrame:
    return _with_combos(box.filter((pl.col("season_type") == REGULAR) & pl.col("played"))
                        ).sort(["player_id", "date"])


def _player_state(played: pl.DataFrame, p: Params) -> pl.DataFrame:
    """Player state AFTER each played game: EWM minutes, EWM of each stat per
    game (for 'teammate out' shares), cumulative within-season sums."""
    cum = [pl.col("min").cum_sum().over(["player_id", "season"]).alias("c_min"),
           pl.col("min").cum_count().over(["player_id", "season"]).alias("c_n")]
    for s in STATS:
        cum.append(pl.col(s).cum_sum().over(["player_id", "season"]).alias(f"c_{s}"))
    ewm = [pl.col("min").ewm_mean(half_life=p.min_half_life).over("player_id").alias("e_min"),
           pl.col("min").alias("l_min"), pl.col("starter").cast(pl.Int8).alias("l_start")]
    for s in ("pts", "reb", "ast", "fg3m", "pra", "pr", "pa", "ra"):
        ewm.append(pl.col(s).ewm_mean(half_life=p.min_half_life).over("player_id").alias(f"e_{s}"))
    st = played.with_columns(cum + ewm)
    return st.select(["player_id", "date", "season", "team", "pos", "e_min", "l_min", "l_start", "c_min", "c_n"]
                     + [f"c_{s}" for s in STATS] + [f"e_{s}" for s in STATS]
                     ).rename({"date": "last_date", "season": "state_season", "team": "last_team"})


def _season_aggs(played: pl.DataFrame) -> pl.DataFrame:
    return played.group_by(["player_id", "season"]).agg(
        [pl.len().alias("g"), pl.col("min").sum().alias("m")] + [pl.col(s).sum().alias(s) for s in STATS])


def _pos_rates(played: pl.DataFrame) -> pl.DataFrame:
    """League per-minute rate by position and season (minutes weighted)."""
    return played.group_by(["season", "pos"]).agg(
        [(pl.col(s).sum() / pl.col("min").sum()).alias(f"pos_{s}") for s in STATS])


def _team_games(box: pl.DataFrame) -> pl.DataFrame:
    """One row per team-game: possessions estimate, points for/against."""
    pl_ = box.filter((pl.col("season_type") == REGULAR) & pl.col("played"))
    tg = pl_.group_by(["game_id", "team", "opp", "date", "season"]).agg(
        pl.col("fga").sum(), pl.col("oreb").sum(), pl.col("tov").sum(), pl.col("fta").sum(),
        pl.col("pts").sum().alias("pf"),
    ).with_columns(poss=pl.col("fga") - pl.col("oreb") + pl.col("tov") + 0.44 * pl.col("fta"))
    opp = tg.select("game_id", pl.col("team").alias("opp"), pl.col("pf").alias("pa"),
                    pl.col("poss").alias("opp_poss"))
    tg = tg.join(opp, on=["game_id", "opp"], how="left").with_columns(
        game_poss=(pl.col("poss") + pl.col("opp_poss")) / 2)
    return tg.sort(["team", "date"])


def _team_state(tg: pl.DataFrame) -> pl.DataFrame:
    """Team pace and net rating AFTER each game (season to date)."""
    return tg.with_columns(
        t_n=pl.col("game_poss").cum_count().over(["team", "season"]),
        t_poss=pl.col("game_poss").cum_sum().over(["team", "season"]),
        t_pf=pl.col("pf").cum_sum().over(["team", "season"]),
        t_pa=pl.col("pa").cum_sum().over(["team", "season"]),
    ).select("team", pl.col("date").alias("t_date"), pl.col("season").alias("t_season"),
             "t_n", "t_poss", "t_pf", "t_pa")


def _team_season(tg: pl.DataFrame) -> pl.DataFrame:
    return tg.group_by(["team", "season"]).agg(
        pl.len().alias("ts_n"), pl.col("game_poss").mean().alias("ts_pace"),
        ((pl.col("pf").sum() - pl.col("pa").sum()) / pl.col("game_poss").sum() * 100).alias("ts_net"))


def _def_state(played: pl.DataFrame, tg: pl.DataFrame) -> pl.DataFrame:
    """What each defense allowed to each position, per 100 possessions,
    season to date, AFTER each game."""
    allowed = played.group_by(["game_id", "opp", "pos", "date", "season"]).agg(
        [pl.col(s).sum() for s in STATS]).rename({"opp": "def_team"})
    poss = tg.select("game_id", pl.col("team").alias("def_team"), "game_poss")
    allowed = allowed.join(poss, on=["game_id", "def_team"], how="left").sort(["def_team", "pos", "date"])
    return allowed.with_columns(
        [pl.col(s).cum_sum().over(["def_team", "pos", "season"]).alias(f"d_{s}") for s in STATS]
        + [pl.col("game_poss").cum_sum().over(["def_team", "pos", "season"]).alias("d_poss"),
           pl.col("game_poss").cum_count().over(["def_team", "pos", "season"]).alias("d_n")]
    ).select(["def_team", "pos", pl.col("date").alias("d_date"), pl.col("season").alias("d_season"),
              "d_poss", "d_n"] + [f"d_{s}" for s in STATS])


def _def_season(played: pl.DataFrame, tg: pl.DataFrame) -> pl.DataFrame:
    allowed = played.group_by(["game_id", "opp", "pos", "season"]).agg(
        [pl.col(s).sum() for s in STATS]).rename({"opp": "def_team"})
    poss = tg.select("game_id", pl.col("team").alias("def_team"), "game_poss")
    a = allowed.join(poss, on=["game_id", "def_team"], how="left")
    per = a.group_by(["def_team", "pos", "season"]).agg(
        [(pl.col(s).sum() / pl.col("game_poss").sum() * 100).alias(f"ds_{s}") for s in STATS])
    lg = a.group_by(["pos", "season"]).agg(
        [(pl.col(s).sum() / pl.col("game_poss").sum() * 100).alias(f"lg_{s}") for s in STATS])
    return per, lg


# ---------------------------------------------------------------------------
# Core
# ---------------------------------------------------------------------------

def _asof(left: pl.DataFrame, right: pl.DataFrame, left_on: str, right_on: str, by) -> pl.DataFrame:
    return left.sort(left_on).join_asof(
        right.sort(right_on), left_on=left_on, right_on=right_on, by=by,
        strategy="backward", allow_exact_matches=False)


def project(box: pl.DataFrame, targets: pl.DataFrame, p: Params | None = None,
            team_dates: pl.DataFrame | None = None, spreads: dict | None = None) -> pl.DataFrame:
    """
    box:     cleaned history (dataio.load_box()), any seasons.
    targets: rows to project, columns player_id, player, team, opp, game_id,
             date, season, home (bool), present (bool: expected to play).
             For the backtest these are box rows; live, they are built from
             the schedule + roster + injury report. Rows with present=False
             are only used to work out minutes/usage freed for teammates.
    team_dates: optional (team, date) of every scheduled game, used for rest
             days on live rows whose previous game isn't in the box yet.
    spreads: optional {game_id: home_spread} from a real market line; when
             missing, the expected margin comes from season net ratings.
    Returns one row per present target with projected minutes, every stat's
    projection, the factors behind it, and distribution parameters.
    """
    p = p or Params()
    played = _played(box)
    tg = _team_games(box)
    state = _player_state(played, p)
    aggs = _season_aggs(played)
    posr = _pos_rates(played)
    tstate = _team_state(tg)
    tseason = _team_season(tg)
    dstate = _def_state(played, tg)
    dseason, dleague = _def_season(played, tg)

    t = targets.with_columns(pl.col("date").cast(pl.Date))
    # --- player state as of the last game before this date
    t = _asof(t, state, "date", "last_date", "player_id")
    t = t.with_columns(
        pl.col("pos").fill_null("F"),
        same_season=(pl.col("state_season") == pl.col("season")).fill_null(False),
        days_since=(pl.col("date") - pl.col("last_date")).dt.total_days(),
    )
    for c in ["c_min", "c_n"] + [f"c_{s}" for s in STATS]:
        t = t.with_columns(pl.when(pl.col("same_season")).then(pl.col(c)).otherwise(0).alias(c))

    # --- prior seasons (season-1 weight w0, season-2 weight w1)
    w0, w1 = p.season_w
    for lag, w in ((1, w0), (2, w1)):
        a = aggs.with_columns((pl.col("season") + lag).alias("season")).rename(
            {"g": f"g{lag}", "m": f"m{lag}", **{s: f"{s}{lag}" for s in STATS}})
        t = t.join(a, on=["player_id", "season"], how="left")
    t = t.with_columns([pl.col(c).fill_null(0) for c in
                        [f"{x}{lag}" for lag in (1, 2) for x in ("g", "m", *STATS)]])
    pr = posr.with_columns((pl.col("season") + 1).alias("season"))
    t = t.join(pr, on=["season", "pos"], how="left")
    # fall back to latest available pos rates if the previous season is missing
    latest_pos = posr.filter(pl.col("season") == posr["season"].max()).drop("season")
    t = t.join(latest_pos, on="pos", how="left", suffix="_latest")
    for s in STATS:
        t = t.with_columns(pl.coalesce(f"pos_{s}", f"pos_{s}_latest").alias(f"pos_{s}"))

    # --- team pace / net rating for both teams, and opponent defense vs position
    ts_prev = tseason.with_columns((pl.col("season") + 1).alias("season"))
    lg_pace = tseason.group_by("season").agg(pl.col("ts_pace").mean().alias("lg_pace")).with_columns(
        (pl.col("season") + 1).alias("season"))
    t = t.join(lg_pace, on="season", how="left").with_columns(
        pl.col("lg_pace").fill_null(tseason["ts_pace"].mean()))
    for side, col in (("own", "team"), ("opp", "opp")):
        st = tstate.rename({"team": col, "t_date": f"{side}_tdate", "t_season": f"{side}_tseason",
                            "t_n": f"{side}_n", "t_poss": f"{side}_poss", "t_pf": f"{side}_pf",
                            "t_pa": f"{side}_pa"})
        t = _asof(t, st, "date", f"{side}_tdate", col)
        prev = ts_prev.rename({"team": col, "ts_n": f"{side}_pn", "ts_pace": f"{side}_ppace",
                               "ts_net": f"{side}_pnet"})
        t = t.join(prev, on=[col, "season"], how="left")
        same = (pl.col(f"{side}_tseason") == pl.col("season")).fill_null(False)
        n = pl.when(same).then(pl.col(f"{side}_n")).otherwise(0)
        cur_pace = pl.when(same).then(pl.col(f"{side}_poss") / pl.col(f"{side}_n")).otherwise(None)
        cur_net = pl.when(same).then((pl.col(f"{side}_pf") - pl.col(f"{side}_pa"))
                                     / pl.col(f"{side}_poss") * 100).otherwise(None)
        prior_pace = (0.5 * pl.col(f"{side}_ppace").fill_null(pl.col("lg_pace")) + 0.5 * pl.col("lg_pace"))
        prior_net = 0.5 * pl.col(f"{side}_pnet").fill_null(0.0)
        t = t.with_columns(
            ((n * cur_pace.fill_null(0) + 15 * prior_pace) / (n + 15)).alias(f"{side}_pace"),
            ((n * cur_net.fill_null(0) + 15 * prior_net) / (n + 15)).alias(f"{side}_net"),
        )

    ds = dstate.rename({"def_team": "opp"})
    t = _asof(t, ds, "date", "d_date", ["opp", "pos"])
    dprev = dseason.rename({"def_team": "opp"}).with_columns((pl.col("season") + 1).alias("season"))
    lprev = dleague.with_columns((pl.col("season") + 1).alias("season"))
    t = t.join(dprev, on=["opp", "pos", "season"], how="left").join(lprev, on=["pos", "season"], how="left")
    d_same = (pl.col("d_season") == pl.col("season")).fill_null(False)
    d_n = pl.when(d_same).then(pl.col("d_n")).otherwise(0)
    for s in STATS:
        lg = pl.col(f"lg_{s}")
        cur = pl.when(d_same & (pl.col("d_poss") > 0)).then(pl.col(f"d_{s}") / pl.col("d_poss") * 100 / lg).otherwise(1.0)
        prior = 0.5 + 0.5 * (pl.col(f"ds_{s}") / lg).fill_null(1.0)
        ratio = ((d_n * cur + p.def_k * prior) / (d_n + p.def_k)).fill_null(1.0)
        t = t.with_columns(ratio.alias(f"def_ratio_{s}"))

    # --- rest days from the team's previous game
    if team_dates is not None and team_dates.height:
        td = team_dates.select("team", pl.col("date").cast(pl.Date).alias("prev_date")).unique()
    else:
        td = tg.select("team", pl.col("date").alias("prev_date")).unique()
    t = _asof(t, td, "date", "prev_date", "team").with_columns(
        rest=(pl.col("date") - pl.col("prev_date")).dt.total_days())

    # --- teammates out: minutes and production freed this game
    rot = (pl.col("e_min") >= p.rotation_min) & (pl.col("days_since") <= p.absent_window_days) \
        & (pl.col("last_team") == pl.col("team"))
    t = t.with_columns(out_flag=(~pl.col("present")) & rot.fill_null(False))
    # Teammates' recent-minutes averages already absorb an absence the longer
    # it lasts: after g missed games, 0.5^(g / half_life) of it is still new.
    missed = ((pl.col("days_since") - pl.col("rest").fill_null(2)).clip(0, None) / 2.1)
    t = t.with_columns(fresh=pl.lit(0.5).pow(missed / p.min_half_life))
    free = t.filter(pl.col("out_flag")).group_by(["game_id", "team"]).agg(
        [(pl.col("e_min") * pl.col("fresh")).sum().alias("free_min"), pl.col("player").alias("out_players")]
        + [(pl.col(f"e_{s}") * pl.col("fresh")).sum().alias(f"free_{s}") for s in STATS])
    t = t.filter(pl.col("present") & pl.col("e_min").is_not_null())
    t = t.join(free, on=["game_id", "team"], how="left").with_columns(
        pl.col("free_min").fill_null(0.0), *[pl.col(f"free_{s}").fill_null(0.0) for s in STATS])
    # Early in a season the recency-weighted average still reflects the end
    # of last season (rest days, tanking, limits). Blend it with last
    # season's per-game minutes until this season has min_prior_k games.
    prev_avg = pl.when(pl.col("g1") > 0).then(pl.col("m1") / pl.col("g1")).otherwise(None)
    n_cur = pl.col("c_n").cast(pl.Float64)
    t = t.with_columns(min_ewm=pl.col("e_min"),
                       min_prev_avg=prev_avg,
                       e_min=pl.when(prev_avg.is_not_null() & (p.min_prior_k > 0))
                       .then((n_cur * pl.col("e_min") + p.min_prior_k * prev_avg) / (n_cur + p.min_prior_k))
                       .otherwise(pl.col("e_min")))
    if p.last_game_w > 0:
        # a role change shows up in the latest game first: lean on it, this season only
        t = t.with_columns(e_min=pl.when(pl.col("same_season") & pl.col("l_min").is_not_null())
                           .then((1 - p.last_game_w) * pl.col("e_min") + p.last_game_w * pl.col("l_min"))
                           .otherwise(pl.col("e_min")))

    # Rotation: fill each team's minutes in order of recent minutes. A
    # player whose higher-minute teammates already cover ROTATION_CAP is
    # "fringe" (a live roster lists ~18 players; ~10 play) and gets only a
    # fraction of his recent minutes, outside the team-total scaling.
    t = t.sort(["game_id", "team", "e_min"], descending=[False, False, True]).with_columns(
        cum_before=(pl.col("e_min").cum_sum() - pl.col("e_min")).over(["game_id", "team"]))
    t = t.with_columns(fringe=pl.col("cum_before") >= p.rotation_cap)
    wt = pl.when((pl.col("e_min") >= 8) & ~pl.col("fringe")).then(pl.col("e_min")).otherwise(0.0)
    t = t.with_columns(w=wt)
    t = t.with_columns(w_sum=pl.col("w").sum().over(["game_id", "team"]))
    team_e = [pl.col(f"e_{s}").sum().over(["game_id", "team"]).alias(f"team_e_{s}") for s in STATS]
    t = t.with_columns(team_e)

    # --- minutes
    base_min = pl.col("e_min")
    if p.min_mode == "normalize":
        # A team plays ~241 minutes (240 + the overtime average). Rotation
        # players' recent minutes are scaled part of the way toward that
        # total, which both hands out minutes freed by absent teammates and
        # trims the excess when everyone is healthy.
        bench = pl.when((pl.col("e_min") < 8) & ~pl.col("fringe")).then(pl.col("e_min")).otherwise(0.0).sum().over(["game_id", "team"])
        target = (241.0 - bench).clip(150, None)
        scale = (target / pl.col("w_sum")).clip(0.8, 1.3)
        extra = pl.when((pl.col("w") > 0) & (pl.col("w_sum") > 0) & p.use_out).then(
            p.redistribute * (scale - 1) * pl.col("w")).otherwise(0.0)
    else:
        extra = pl.when((pl.col("w_sum") > 0) & p.use_out).then(
            p.redistribute * pl.col("free_min") * pl.col("w") / pl.col("w_sum")).otherwise(0.0)
    b2b = (pl.col("rest") == 1).fill_null(False)
    b2b_cut = pl.when(b2b & (base_min >= p.b2b_min_threshold) & p.use_home_rest).then(
        p.b2b_min * base_min).otherwise(0.0)
    margin = None
    if spreads:
        sp = pl.DataFrame({"game_id": list(spreads), "home_spread": [float(v) for v in spreads.values()]},
                          schema={"game_id": pl.Int64, "home_spread": pl.Float64})
        t = t.join(sp, on="game_id", how="left")
    else:
        t = t.with_columns(home_spread=pl.lit(None, dtype=pl.Float64))
    est_margin = (pl.col("own_net") - pl.col("opp_net")) * pl.col("lg_pace") / 100 \
        + pl.when(pl.col("home")).then(2.5).otherwise(-2.5)
    mkt_margin = pl.when(pl.col("home")).then(-pl.col("home_spread")).otherwise(pl.col("home_spread"))
    t = t.with_columns(exp_margin=pl.coalesce(mkt_margin, est_margin),
                       margin_source=pl.when(pl.col("home_spread").is_not_null()).then(pl.lit("market"))
                       .otherwise(pl.lit("ratings")))
    blow = pl.when((base_min >= 28) & p.use_blowout).then(
        pl.min_horizontal(p.blowout_cap, p.blowout_c * (pl.col("exp_margin").abs() - p.blowout_threshold).clip(0, None))
    ).otherwise(0.0)
    fringe_cut = pl.when(pl.col("fringe")).then(-(1 - p.fringe_share) * base_min).otherwise(0.0)
    t = t.with_columns(min_base=base_min, min_extra=extra + fringe_cut, min_b2b=b2b_cut, min_blowout=-blow)
    t = t.with_columns(proj_min=(pl.col("min_base") + pl.col("min_extra") + pl.col("min_b2b")
                                 + pl.col("min_blowout")).clip(0, p.min_cap))
    # Hard team cap: expected minutes can't add up to more than a game has.
    # Only ever scales DOWN, and only non-fringe players.
    team_sum = pl.col("proj_min").sum().over(["game_id", "team"])
    fringe_sum = pl.when(pl.col("fringe")).then(pl.col("proj_min")).otherwise(0.0).sum().over(["game_id", "team"])
    if p.cap_mode == "waterfall":
        # Minutes go to players in order of their projection; whatever no
        # longer fits under the cap comes off the deepest players first.
        t = t.sort(["game_id", "team", "proj_min"], descending=[False, False, True]).with_columns(
            cum_prev=(pl.col("proj_min").cum_sum() - pl.col("proj_min")).over(["game_id", "team"]))
        room = (p.team_min_cap - pl.col("cum_prev")).clip(0, None)
        t = t.with_columns(min_cap_adj=pl.when(team_sum > p.team_min_cap)
                           .then(pl.min_horizontal(pl.col("proj_min"), room) - pl.col("proj_min")).otherwise(0.0))
    else:
        cap_scale = ((p.team_min_cap - fringe_sum) / (team_sum - fringe_sum)).clip(None, 1.0)
        t = t.with_columns(min_cap_adj=pl.when(~pl.col("fringe") & (team_sum > p.team_min_cap))
                           .then(pl.col("proj_min") * (cap_scale - 1)).otherwise(0.0))
    t = t.with_columns(proj_min=pl.col("proj_min") + pl.col("min_cap_adj"),
                       min_extra=pl.col("min_extra") + pl.col("min_cap_adj"))

    # --- context factors (shared and per stat)
    cap = p.cap
    def capped(e):
        return (1 + e).clip(1 - cap, 1 + cap)
    f_pace = capped(p.a_pace * (pl.col("opp_pace") / pl.col("lg_pace") - 1)) if p.use_pace else pl.lit(1.0)
    f_home = (pl.when(pl.col("home")).then(1 + p.home).otherwise(1 - p.home)) if p.use_home_rest else pl.lit(1.0)
    f_rest = (pl.when(b2b).then(1 + p.b2b_rate).when((pl.col("rest") >= 3).fill_null(False))
              .then(1 + p.rest3_rate).otherwise(1.0)) if p.use_home_rest else pl.lit(1.0)
    t = t.with_columns(f_pace=f_pace, f_home=f_home, f_rest=f_rest)

    out_cols = []
    for s in STATS:
        k = p.k[s]
        n = pl.col("c_n")
        cur = pl.when(pl.col("c_min") > 0).then(pl.col(f"c_{s}") / pl.col("c_min")).otherwise(0.0)
        pm = w0 * pl.col("m1") + w1 * pl.col("m2")
        pg = w0 * pl.col("g1") + w1 * pl.col("g2")
        prior_raw = pl.when(pm > 0).then((w0 * pl.col(f"{s}1") + w1 * pl.col(f"{s}2")) / pm).otherwise(0.0)
        prior = (pg * prior_raw + p.k_pos * pl.col(f"pos_{s}")) / (pg + p.k_pos)
        rate = (n * cur + k * prior) / (n + k)
        f_def = capped(p.a_def * (pl.col(f"def_ratio_{s}") - 1)) if p.use_def else pl.lit(1.0)
        share_out = pl.when(pl.col(f"team_e_{s}") > 0).then(pl.col(f"free_{s}") / (pl.col(f"team_e_{s}") + pl.col(f"free_{s}"))).otherwise(0.0)
        f_use = ((1 + p.usage_b * share_out).clip(1.0, 1 + p.usage_cap)) if p.use_out else pl.lit(1.0)
        out_cols += [
            rate.alias(f"rate_{s}"), prior.alias(f"prior_rate_{s}"),
            cur.alias(f"cur_rate_{s}"), (n / (n + k)).alias(f"w_cur_{s}"),
            f_def.alias(f"f_def_{s}"), f_use.alias(f"f_use_{s}"),
        ]
    t = t.with_columns(out_cols)
    t = t.with_columns([
        (pl.col("proj_min") * pl.col(f"rate_{s}") * pl.col("f_pace") * pl.col("f_home") * pl.col("f_rest")
         * pl.col(f"f_def_{s}") * pl.col(f"f_use_{s}")).alias(f"proj_{s}")
        for s in STATS])

    # --- shared-production check. Each player's rate was earned in his own
    # role; put several lead options on one roster and their projections add
    # up to more points/rebounds/assists than a team produces. Compare the
    # team's summed projection with a norm (half league average, half this
    # team's own last season) and move each player part of the way.
    base_stats = ("pts", "reb", "ast", "fg3m")
    tg_all = box.filter((pl.col("season_type") == REGULAR) & pl.col("played")).group_by(
        ["game_id", "team", "season"]).agg([pl.col(s).sum() for s in base_stats])
    lg_team = tg_all.group_by("season").agg([pl.col(s).mean().alias(f"lgt_{s}") for s in base_stats]) \
        .with_columns((pl.col("season") + 1).alias("season"))
    own_team = tg_all.group_by(["team", "season"]).agg([pl.col(s).mean().alias(f"own_{s}") for s in base_stats]) \
        .with_columns((pl.col("season") + 1).alias("season"))
    t = t.join(lg_team, on="season", how="left").join(own_team, on=["team", "season"], how="left")
    for s in base_stats:
        lgv = pl.col(f"lgt_{s}").fill_null(tg_all[s].mean())
        norm = 0.5 * lgv + 0.5 * pl.col(f"own_{s}").fill_null(lgv)
        tot = pl.col(f"proj_{s}").sum().over(["game_id", "team"])
        ratio = (norm * pl.col("f_pace") / tot).clip(0.5, 2.0)
        f = pl.when(tot > 0).then(ratio.pow(p.team_share_alpha)).otherwise(1.0).clip(1 - p.team_share_cap, 1 + p.team_share_cap)
        t = t.with_columns(f.alias(f"f_team_{s}"))
    old_parts = {s: pl.col(f"proj_{s}") for s in base_stats}
    t = t.with_columns([(pl.col(f"proj_{s}") * pl.col(f"f_team_{s}")).alias(f"proj_{s}") for s in base_stats]
                       + [pl.col(f"proj_{s}").alias(f"pre_team_{s}") for s in base_stats])
    for s, parts in STATS.items():
        if len(parts) > 1:
            old = sum(pl.col(f"pre_team_{x}") for x in parts)
            new = sum(pl.col(f"proj_{x}") for x in parts)
            t = t.with_columns(pl.when(old > 0).then(pl.col(f"proj_{s}") * new / old)
                               .otherwise(pl.col(f"proj_{s}")).alias(f"proj_{s}"),
                               pl.when(old > 0).then(new / old).otherwise(1.0).alias(f"f_team_{s}"))
    return t


# ---------------------------------------------------------------------------
# Distributions
# ---------------------------------------------------------------------------

def fit_variance(df: pl.DataFrame, stat: str) -> dict:
    """Least-squares fit of residual variance = phi*mu + kappa*mu^2 by
    projected-value bucket. Returns {"phi", "kappa"}."""
    d = df.select(pl.col(f"proj_{stat}").alias("mu"), (pl.col(stat) - pl.col(f"proj_{stat}")).pow(2).alias("r2"))
    d = d.filter(pl.col("mu") > 0.2)
    b = d.with_columns(bucket=(pl.col("mu") * 4).round() / 4).group_by("bucket").agg(
        pl.col("mu").mean(), pl.col("r2").mean(), pl.len().alias("n")).filter(pl.col("n") >= 30)
    # weighted least squares, no intercept: r2 = phi*mu + kappa*mu^2
    sxx = syy = sxy = sx1 = sx2 = 0.0
    for mu, r2, n in b.select("mu", "r2", "n").iter_rows():
        x1, x2 = mu, mu * mu
        sx1 += n * x1 * x1; sx2 += n * x2 * x2; sxy += n * x1 * x2
        sxx += n * x1 * r2; syy += n * x2 * r2
    det = sx1 * sx2 - sxy * sxy
    if det <= 0:
        return {"phi": 1.0, "kappa": 0.05}
    phi = (sxx * sx2 - syy * sxy) / det
    kappa = (syy * sx1 - sxx * sxy) / det
    return {"phi": max(phi, 0.5), "kappa": max(kappa, 0.0)}


def player_var_mult(df: pl.DataFrame, stat: str, vf: dict, k: float) -> pl.DataFrame:
    """Per-player variance multiplier from his own PRIOR misses:
    mean of resid^2 / model variance over his earlier games, shrunk to 1
    with k games. Shifted so a row never uses its own result."""
    mu = pl.col(f"proj_{stat}")
    var = vf["phi"] * mu + vf["kappa"] * mu * mu
    d = df.sort(["player_id", "date"]).with_columns(
        z2=((pl.col(stat) - mu).pow(2) / var.clip(0.25, None)))
    d = d.with_columns(
        n_prev=pl.col("z2").cum_count().shift(1, fill_value=0).over("player_id"),
        s_prev=pl.col("z2").cum_sum().shift(1, fill_value=0.0).over("player_id"),
    )
    return d.with_columns(
        ((pl.col("s_prev") + k) / (pl.col("n_prev") + k)).clip(0.6, 1.8).alias(f"vmult_{stat}"))


def _nb_cdf(x: int, mu: float, var: float) -> float:
    """P(X <= x) for a negative binomial with mean mu and variance var
    (falls back to Poisson when var <= mu)."""
    if x < 0:
        return 0.0
    if mu <= 0:
        return 1.0
    if var <= mu * 1.0001:
        term = math.exp(-mu); tot = term
        for i in range(1, x + 1):
            term *= mu / i; tot += term
        return min(tot, 1.0)
    r = mu * mu / (var - mu)
    q = mu / var  # success prob
    term = q ** r
    tot = term
    for i in range(1, x + 1):
        term *= (i - 1 + r) / i * (1 - q)
        tot += term
    return min(tot, 1.0)


def prob_over(stat: str, mu: float, var: float, line: float) -> float | None:
    """Probability the result goes OVER `line` (strictly greater). Half-point
    lines never push; on a whole-number line a push is excluded from both
    sides, so the result is P(over) / (P(over) + P(under))."""
    if mu is None or var is None or line is None or var <= 0:
        return None
    if stat in COUNT_STATS:
        if float(line).is_integer():
            le = _nb_cdf(int(line), mu, var)
            lt = _nb_cdf(int(line) - 1, mu, var)
            p_over, p_under = 1 - le, lt
            tot = p_over + p_under
            return p_over / tot if tot > 0 else None
        return 1 - _nb_cdf(math.floor(line), mu, var)
    sd = math.sqrt(var)
    # continuity correction: results are whole numbers
    if float(line).is_integer():
        p_over = 1 - _phi((line + 0.5 - mu) / sd)
        p_under = _phi((line - 0.5 - mu) / sd)
        tot = p_over + p_under
        return p_over / tot if tot > 0 else None
    return 1 - _phi((math.floor(line) + 0.5 - mu) / sd)


def _phi(z: float) -> float:
    return 0.5 * (1 + math.erf(z / math.sqrt(2)))


def quantile(stat: str, mu: float, var: float, q: float) -> float:
    if stat in COUNT_STATS:
        x = 0
        while _nb_cdf(x, mu, var) < q and x < 200:
            x += 1
        return float(x)
    # inverse normal by bisection
    lo, hi = -8.0, 8.0
    for _ in range(60):
        mid = (lo + hi) / 2
        if _phi(mid) < q:
            lo = mid
        else:
            hi = mid
    return mu + lo * math.sqrt(var)
