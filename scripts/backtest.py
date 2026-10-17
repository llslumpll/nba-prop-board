"""
Walk-forward backtest of model.py on full past seasons.

Every projection for a game uses only games before that date. Constants are
tuned on 2024-25 (season 2025) and then scored, untouched, on 2025-26
(season 2026), so the reported test numbers aren't fit to the season they
are measured on.

No free source has historical prop lines, so this can't measure us against
the market. It measures:
  * average error (MAE) and bias vs three simple baselines:
      season_avg  -- season-to-date average (last season's average before game 1)
      last10      -- average of the last 10 games played
      nfl_style   -- the NFL site's formula: career avg + 0.20 * (season avg - career avg)
  * distribution calibration: how often the result lands inside the model's
    50% and 80% ranges (should be ~50% and ~80%)
  * a "naive line" check: a line set at the season-average baseline
    (floor + 0.5), and how often the model's side of it wins. This is a
    sanity check only -- real books set sharper lines than this.

Comparison against real market lines happens live, on the History page.

Usage:  python scripts/backtest.py            # tune on 2025, test on 2026, write data/backtest.json
"""

from __future__ import annotations

import json
import math
import random
import sys
import time
from dataclasses import replace
from pathlib import Path

import polars as pl

sys.path.insert(0, str(Path(__file__).parent))
import dataio  # noqa: E402
import model  # noqa: E402
from model import STATS, Params  # noqa: E402

OUT = Path(__file__).parent.parent / "data" / "backtest.json"
PROP_MIN = 20.0  # "prop-relevant": players projected for 20+ minutes
K_GRID = (2, 3, 5, 8, 12, 20)


def targets_for(box: pl.DataFrame, season: int) -> pl.DataFrame:
    """Played rows of `season` (present=True) plus one present=False row for
    every rotation player who played for that team in its previous 5 games
    but has no played row in this one (out, inactive or DNP)."""
    reg = box.filter((pl.col("season_type") == model.REGULAR) & (pl.col("season") == season))
    cols = ["player_id", "player", "team", "opp", "game_id", "date", "season", "home"]
    present = reg.filter(pl.col("played")).select(cols).with_columns(present=pl.lit(True))

    games = reg.select("game_id", "team", "opp", "date", "home", "season").unique().sort(["team", "date"])
    games = games.with_columns(g_idx=pl.col("date").rank("dense").over("team"))
    played = reg.filter(pl.col("played")).select("player_id", "player", "team", "game_id").join(
        games.select("game_id", "team", "g_idx"), on=["game_id", "team"])
    cand = []
    for back in range(1, 6):
        cand.append(played.with_columns((pl.col("g_idx") + back).alias("g_idx")).select("player_id", "player", "team", "g_idx"))
    cand = pl.concat(cand).unique(["player_id", "team", "g_idx"])
    cand = cand.join(games, on=["team", "g_idx"]).select(cols)
    absent = cand.join(present.select("player_id", "game_id"), on=["player_id", "game_id"], how="anti").with_columns(
        present=pl.lit(False))
    return pl.concat([present, absent], how="vertical_relaxed")


def attach_actuals(proj: pl.DataFrame, box: pl.DataFrame) -> pl.DataFrame:
    act = model._with_combos(box.filter(pl.col("played"))).select(
        ["player_id", "game_id", "min"] + list(STATS))
    return proj.join(act, on=["player_id", "game_id"], how="inner")


def baselines(box: pl.DataFrame, season: int) -> pl.DataFrame:
    """season_avg / last10 / nfl_style baselines for every played row of
    `season`, from prior games only."""
    played = model._with_combos(box.filter((pl.col("season_type") == model.REGULAR) & pl.col("played"))
                                ).sort(["player_id", "date"])
    exprs = []
    for s in STATS:
        exprs += [
            (pl.col(s).cum_sum().shift(1).over(["player_id", "season"])
             / pl.col(s).cum_count().shift(1).over(["player_id", "season"])).alias(f"sa_{s}"),
            pl.col(s).rolling_mean(10, min_samples=1).shift(1).over("player_id").alias(f"l10_{s}"),
        ]
    played = played.with_columns(exprs)
    prev = played.group_by(["player_id", "season"]).agg([pl.col(s).mean().alias(f"prev_{s}") for s in STATS]) \
        .with_columns((pl.col("season") + 1).alias("season"))
    career = (played.filter(pl.col("season") < season).group_by("player_id")
              .agg([pl.col(s).mean().alias(f"car_{s}") for s in STATS]))
    out = played.filter(pl.col("season") == season).join(prev, on=["player_id", "season"], how="left") \
        .join(career, on="player_id", how="left")
    cols = []
    for s in STATS:
        sa = pl.coalesce(f"sa_{s}", f"prev_{s}")
        car = pl.coalesce(f"car_{s}", sa)
        cols += [sa.alias(f"b_season_avg_{s}"), pl.col(f"l10_{s}").alias(f"b_last10_{s}"),
                 (car + 0.20 * (pl.col(f"sa_{s}").fill_null(car) - car)).alias(f"b_nfl_style_{s}")]
    return out.select(["player_id", "game_id"] + cols)


def run_projection(box, targets, p):
    return attach_actuals(model.project(box, targets, p), box)


def team_fit(df: pl.DataFrame, box: pl.DataFrame, season: int) -> dict:
    """Team scores from summed player points: scale factor, and how far off
    margin and total have been (used for game-level chances)."""
    team = df.group_by(["game_id", "team", "home"]).agg(pl.col("proj_pts").sum().alias("proj"))
    act = box.filter(pl.col("played") & (pl.col("season") == season) & (pl.col("season_type") == model.REGULAR)) \
        .group_by(["game_id", "team"]).agg(pl.col("pts").sum().alias("act"))
    j = team.join(act, on=["game_id", "team"])
    h = j.filter(pl.col("home")).select("game_id", pl.col("proj").alias("hp"), pl.col("act").alias("ha"))
    a = j.filter(~pl.col("home")).select("game_id", pl.col("proj").alias("ap"), pl.col("act").alias("aa"))
    g = h.join(a, on="game_id")
    k = (g["ha"].sum() + g["aa"].sum()) / (g["hp"].sum() + g["ap"].sum())
    g = g.with_columns(m=(pl.col("ha") - pl.col("aa")) - k * (pl.col("hp") - pl.col("ap")),
                       t=(pl.col("ha") + pl.col("aa")) - k * (pl.col("hp") + pl.col("ap")),
                       ok=((pl.col("hp") - pl.col("ap")) > 0) == (pl.col("ha") > pl.col("aa")))
    return {"games": g.height, "scale": round(k, 4), "margin_bias": round(g["m"].mean(), 2),
            "margin_sd": round(g["m"].std(), 2), "total_sd": round(g["t"].std(), 2),
            "winner_pct": round(100 * g["ok"].mean(), 1)}


def mae(df: pl.DataFrame, col: str, stat: str) -> float:
    return df.select((pl.col(col) - pl.col(stat)).abs().mean()).item()


def scoreboard(df: pl.DataFrame, stats=STATS) -> dict:
    out = {}
    for s in stats:
        d = df.filter(pl.col(f"proj_{s}").is_not_null() & pl.col(f"b_season_avg_{s}").is_not_null()
                      & pl.col(f"b_last10_{s}").is_not_null())
        row = {"n": d.height}
        for name, col in (("model", f"proj_{s}"), ("season_avg", f"b_season_avg_{s}"),
                          ("last10", f"b_last10_{s}"), ("nfl_style", f"b_nfl_style_{s}")):
            row[name] = {"mae": round(mae(d, col, s), 3),
                         "bias": round(d.select((pl.col(col) - pl.col(s)).mean()).item(), 3)}
        out[s] = row
    return out


def minutes_board(df: pl.DataFrame) -> dict:
    d = df.filter(pl.col("proj_min").is_not_null())
    return {"n": d.height, "mae": round(mae(d, "proj_min", "min"), 2),
            "bias": round(d.select((pl.col("proj_min") - pl.col("min")).mean()).item(), 2),
            "ewm_only_mae": round(mae(d, "min_base", "min"), 2)}


def calibration(df: pl.DataFrame, vfs: dict, p: Params) -> dict:
    """Interval coverage and the naive-line check, per stat."""
    out = {}
    for s in STATS:
        d = model.player_var_mult(df.filter(pl.col(f"proj_{s}").is_not_null()), s, vfs[s], p.var_k)
        d = d.filter(pl.col(f"b_season_avg_{s}").is_not_null())
        if d.height > 6000:
            d = d.sample(6000, seed=7)
        in50 = in80 = 0
        rng = random.Random(11)
        calls = hits = strong = strong_hits = 0
        brier = 0.0
        for mu, v_mult, actual, base in d.select(f"proj_{s}", f"vmult_{s}", s, f"b_season_avg_{s}").iter_rows():
            var = (vfs[s]["phi"] * mu + vfs[s]["kappa"] * mu * mu) * v_mult
            if var <= 0:
                continue
            # randomized PIT: for whole-number stats a result sitting exactly on
            # a range edge is split, so the check isn't inflated by ties
            if s in model.COUNT_STATS:
                below = model._nb_cdf(int(actual) - 1, mu, var)
                at = model._nb_cdf(int(actual), mu, var) - below
                u = below + rng.random() * at
            else:
                u = model._phi((actual - mu) / math.sqrt(var))
            in50 += 0.25 <= u <= 0.75
            in80 += 0.10 <= u <= 0.90
            line = int(base) + 0.5
            po = model.prob_over(s, mu, var, line)
            if po is None:
                continue
            went_over = actual > line
            brier += (po - went_over) ** 2
            calls += 1
            hits += (po >= 0.5) == went_over
            if abs(po - 0.5) >= 0.08:
                strong += 1
                strong_hits += (po >= 0.5) == went_over
        n = d.height
        out[s] = {
            "n": n,
            "in_50pct_range": round(100 * in50 / n, 1),
            "in_80pct_range": round(100 * in80 / n, 1),
            "naive_line_hit_rate_skew_driven": round(100 * hits / calls, 1) if calls else None,
            "naive_line_strong_calls": strong,
            "naive_line_strong_hit_rate": round(100 * strong_hits / strong, 1) if strong else None,
            "brier": round(brier / calls, 4) if calls else None,
        }
    return out


def main():
    t0 = time.time()
    box = dataio.load_box()
    box = box.filter(pl.col("season") < dataio.CURRENT_SEASON)
    results = {"tuned_on": 2025, "tested_on": 2026, "prop_relevant_min": PROP_MIN, "steps": []}

    tune_t = targets_for(box, 2025)
    tune_b = baselines(box, 2025)

    def evaluate(p, label):
        df = run_projection(box, tune_t, p).join(tune_b, on=["player_id", "game_id"], how="left")
        df = df.filter(pl.col("proj_min") >= PROP_MIN)
        sb = scoreboard(df)
        print(f"[{time.time()-t0:5.0f}s] {label}: " + "  ".join(f"{s} {sb[s]['model']['mae']:.3f}" for s in STATS))
        return df, sb

    # 1) k per stat
    base = Params()
    best_k = {}
    k_table = {}
    for kv in K_GRID:
        p = replace(base, k={s: kv for s in STATS})
        _, sb = evaluate(p, f"k={kv}")
        for s in STATS:
            k_table.setdefault(s, {})[kv] = sb[s]["model"]["mae"]
    for s in STATS:
        best_k[s] = min(k_table[s], key=k_table[s].get)
    results["k_grid_mae_2025"] = k_table
    results["best_k"] = best_k
    p = replace(base, k=best_k)
    _, ref = evaluate(p, "tuned k")

    # 2) ablations: does each context piece help? keep it only if it lowers MAE on 2025
    ablations = {}
    for flag in ("use_pace", "use_def", "use_home_rest", "use_out", "use_blowout"):
        _, sb = evaluate(replace(p, **{flag: False}), f"without {flag}")
        delta = {s: round(sb[s]["model"]["mae"] - ref[s]["model"]["mae"], 4) for s in STATS}
        ablations[flag] = delta  # positive = removing it makes things worse = it helps
    results["ablation_mae_change_when_removed_2025"] = ablations
    keep = {flag: (sum(ablations[flag].values()) >= 0) for flag in ablations}
    p = replace(p, **keep)
    results["kept_context"] = keep

    # 3) a few strength settings for the pieces that stayed
    tweaks = {}
    for name, field_, vals in (("minutes half-life", "min_half_life", (5.0, 8.0, 12.0)),
                               ("team-minutes pull", "redistribute", (0.3, 0.5, 0.7, 1.0)),
                               ("usage_b", "usage_b", (0.0, 0.1, 0.2)),
                               ("a_def", "a_def", (0.25, 0.5, 0.75)),
                               ("shared-production pull", "team_share_alpha", (0.25, 0.5, 0.75))):
        scores = {}
        for v in vals:
            _, sb = evaluate(replace(p, **{field_: v}), f"{field_}={v}")
            scores[v] = round(sum(sb[s]["model"]["mae"] for s in ("pts", "reb", "ast", "fg3m")), 4)
        best = min(scores, key=scores.get)
        tweaks[name] = {"scores_sum_mae": {str(k): v for k, v in scores.items()}, "chosen": best}
        p = replace(p, **{field_: best})
    results["tuning"] = tweaks

    # variance fit on the tuning season
    tune_df, _ = evaluate(p, "final params on 2025")
    vfs = {s: model.fit_variance(tune_df, s) for s in STATS}
    results["variance_fit"] = vfs
    results["params"] = {k: v for k, v in p.__dict__.items()}

    # 4) test season, untouched
    test_t = targets_for(box, 2026)
    test_b = baselines(box, 2026)
    test = run_projection(box, test_t, p).join(test_b, on=["player_id", "game_id"], how="left")
    test_prop = test.filter(pl.col("proj_min") >= PROP_MIN)
    results["test_2026"] = {
        "prop_relevant": scoreboard(test_prop),
        "all_rotation": scoreboard(test.filter(pl.col("proj_min") >= 10)),
        "minutes": minutes_board(test_prop),
        "calibration": calibration(test_prop, vfs, p),
    }
    results["team_fit_2026"] = team_fit(test, box, 2026)
    results["runtime_s"] = round(time.time() - t0, 1)
    OUT.write_text(json.dumps(results, indent=2, default=str))
    print(json.dumps({"best_k": best_k, "kept": keep, "test": results["test_2026"]}, indent=1, default=str))


if __name__ == "__main__":
    main()
