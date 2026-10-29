"""
With / without ("WOWY") adjustments for absences, from real game history.

Two effects, both computed only from games BEFORE the projected date (so the
backtest never peeks), over the current and previous season:

  teammate out   For player A and absent teammate B: A's per-game stats in
                 games B missed (while on the team) minus games they shared.
                 Shrunk by n_without / (n_without + 6); needs 3+ games each
                 way. Added to A's projection (weight Params.wowy_w), scaled
                 by how "fresh" B's absence is (an absence that's lasted
                 weeks is already in A's recent averages).

  opponent out   For the opponent missing player B: what that team allowed
                 per game (points, rebounds, assists, threes) without B vs
                 with him, as a ratio, shrunk the same way and capped at
                 +/-10%. Multiplies A's projection (weight Params.opp_out_w).

Both weights start at 0 (off) and are switched on only by the backtest /
self-tuning when they lower the projection miss on games they weren't
chosen on.
"""

from __future__ import annotations

from collections import defaultdict

import numpy as np
import polars as pl

BASE = ("pts", "reb", "ast", "fg3m")
SHRINK = 6.0
MIN_N = 3
OPP_CAP = 0.10


def _team_tables(played: pl.DataFrame, team: str, seasons: list[int]):
    g = played.filter((pl.col("team") == team) & pl.col("season").is_in(seasons))
    if not g.height:
        return None
    games = g.select("game_id", "date", "season").unique().sort(["date", "game_id"])
    gidx = {gid: i for i, gid in enumerate(games["game_id"].to_list())}
    pids = sorted(set(g["player_id"].to_list()))
    pidx = {pid: j for j, pid in enumerate(pids)}
    G, N = games.height, len(pids)
    P = np.zeros((G, N), dtype=bool)
    X = np.zeros((G, N, len(BASE)))
    M = np.zeros((G, N))
    for r in g.select(["game_id", "player_id", "min", *BASE]).iter_rows():
        i, j = gidx[r[0]], pidx[r[1]]
        P[i, j] = True
        M[i, j] = r[2] or 0.0
        X[i, j] = r[3:]
    first = np.where(P.any(axis=0), P.argmax(axis=0), G)
    dates = np.array(games["date"].to_list(), dtype="datetime64[D]")
    return {"P": P, "X": X, "M": M, "pidx": pidx, "first": first, "dates": dates, "G": G}


def _allowed(box: pl.DataFrame) -> pl.DataFrame:
    """Per team-game: what the opponent produced (team totals)."""
    tot = box.group_by(["game_id", "team"]).agg([pl.col(s).sum() for s in BASE])
    pairs = box.select("game_id", "team", "opp").unique()
    return pairs.join(tot.rename({"team": "opp", **{s: f"a_{s}" for s in BASE}}), on=["game_id", "opp"], how="left")


def adjustments(box: pl.DataFrame, rows: pl.DataFrame, outs: pl.DataFrame) -> pl.DataFrame:
    """rows: present targets (game_id, player_id, team, opp, date, season).
    outs: absent rotation players (game_id, team, player_id, fresh).
    Returns game_id, player_id, wowy_<stat> (additive, PER MINUTE) and oppf_<stat> (ratio) for BASE stats."""
    empty = pl.DataFrame(schema={"game_id": pl.Int64, "player_id": pl.Int64,
                                 **{f"wowy_{s}": pl.Float64 for s in BASE}, **{f"oppf_{s}": pl.Float64 for s in BASE}})
    if not rows.height or not outs.height:
        return empty
    played = box.filter(pl.col("played") & (pl.col("season_type") == 2))
    out_by = defaultdict(list)
    for gid, team, pid, fresh in outs.select("game_id", "team", "player_id", "fresh").iter_rows():
        out_by[(gid, team)].append((pid, float(fresh if fresh is not None else 1.0)))
    tables = {}

    def table(team, season):
        k = (team, season)
        if k not in tables:
            tables[k] = _team_tables(played, team, [season - 1, season])
        return tables[k]

    # opponent allowed per game, per team (current + previous season)
    allowed = _allowed(played)
    allowed_tbl = {}

    def opp_ratio(team, season, date, b, fresh):
        k = (team, season)
        if k not in allowed_tbl:
            tb = table(team, season)
            if tb is None:
                allowed_tbl[k] = None
            else:
                a = allowed.filter(pl.col("team") == team).select("game_id", *[f"a_{s}" for s in BASE])
                amap = {r[0]: r[1:] for r in a.iter_rows()}
                gids = played.filter((pl.col("team") == team) & pl.col("season").is_in([season - 1, season])) \
                    .select("game_id", "date").unique().sort(["date", "game_id"])["game_id"].to_list()
                allowed_tbl[k] = np.array([amap.get(g, (np.nan,) * len(BASE)) for g in gids], dtype=float)
        A = allowed_tbl[k]
        tb = table(team, season)
        if A is None or tb is None or b not in tb["pidx"]:
            return None
        cut = int(np.searchsorted(tb["dates"], np.datetime64(date, "D")))
        jb = tb["pidx"][b]
        on = np.arange(tb["G"]) >= tb["first"][jb]
        sl = slice(0, cut)
        with_b = tb["P"][sl, jb]
        wo = (~with_b) & on[sl]
        nw, nwo = int(with_b.sum()), int(wo.sum())
        if nw < MIN_N or nwo < MIN_N:
            return None
        k_ = nwo / (nwo + SHRINK) * fresh
        mw, mwo = np.nanmean(A[sl][with_b], axis=0), np.nanmean(A[sl][wo], axis=0)
        r = np.where(mw > 0, mwo / mw, 1.0)
        return np.clip(1 + k_ * (r - 1), 1 - OPP_CAP, 1 + OPP_CAP)

    opp_cache = {}
    out = []
    for gid, pid, team, opp, date, season in rows.select("game_id", "player_id", "team", "opp", "date", "season").iter_rows():
        bump = np.zeros(len(BASE))
        ratio = np.ones(len(BASE))
        tb = table(team, season)
        mine = out_by.get((gid, team), [])
        if tb is not None and mine and pid in tb["pidx"]:
            ja = tb["pidx"][pid]
            cut = int(np.searchsorted(tb["dates"], np.datetime64(date, "D")))
            a_played = tb["P"][:cut, ja]
            for b, fresh in mine:
                if b not in tb["pidx"] or b == pid:
                    continue
                jb = tb["pidx"][b]
                on = np.arange(cut) >= tb["first"][jb]
                b_played = tb["P"][:cut, jb]
                w = a_played & b_played
                wo = a_played & ~b_played & on
                nw, nwo = int(w.sum()), int(wo.sum())
                if nw < MIN_N or nwo < MIN_N:
                    continue
                k_ = nwo / (nwo + SHRINK)
                xa, ma = tb["X"][:cut, ja], tb["M"][:cut, ja]
                mw, mwo = ma[w].sum(), ma[wo].sum()
                if mw <= 0 or mwo <= 0:
                    continue
                # per-MINUTE change (the model already moves minutes around): returned per minute
                d = xa[wo].sum(axis=0) / mwo - xa[w].sum(axis=0) / mw
                bump += fresh * k_ * d
        for b, fresh in out_by.get((gid, opp), []):
            ck = (gid, opp, b)
            if ck not in opp_cache:
                opp_cache[ck] = opp_ratio(opp, season, date, b, fresh)
            r = opp_cache[ck]
            if r is not None:
                ratio *= r
        ratio = np.clip(ratio, 1 - OPP_CAP, 1 + OPP_CAP)
        if bump.any() or (ratio != 1).any():
            out.append((gid, pid, *bump.tolist(), *ratio.tolist()))
    if not out:
        return empty
    return pl.DataFrame(out, schema=empty.schema, orient="row")
