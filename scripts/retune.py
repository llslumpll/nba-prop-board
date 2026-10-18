"""
Monthly retune: re-fits the model's settings with this season's games
included, and adopts the new settings ONLY if they beat the current ones on
games the new settings never saw.

  tune set   = the last complete season + the first 70% of this season's dates
  test set   = the last 30% of this season's dates (held out)
  candidate  = same search the pre-launch backtest ran (k per stat, minutes
               half-life, team-minutes pull, usage, defense strength,
               shared-production pull), fit on the tune set
  adopt if   the candidate's average miss on the test set is at least
             MIN_GAIN lower than the current settings' (averaged over points,
             rebounds, assists and threes, each relative to current), and
             no single one of those stats gets worse by more than MAX_LOSS.

Skips (and says why) until this season has MIN_TEST_ROWS prop-relevant
player-games in the held-out slice -- roughly 3-4 weeks in.

Every run, adopted or not, is appended to data/retune_log.json and shown on
History. On adoption, data/backtest.json's params and variance fit are
replaced, and the old ones are kept in the log so a change can be undone.

Usage:  python scripts/retune.py           # decide and (maybe) adopt
        python scripts/retune.py --dry-run # decide, change nothing
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import asdict, replace
from datetime import datetime, timezone
from pathlib import Path

import polars as pl

sys.path.insert(0, str(Path(__file__).parent))
import backtest  # noqa: E402
import dataio  # noqa: E402
import live  # noqa: E402
import model  # noqa: E402
from model import STATS  # noqa: E402

DATA = Path(__file__).parent.parent / "data"
BT_PATH = DATA / "backtest.json"
LOG_PATH = DATA / "retune_log.json"

TEST_SHARE = 0.30
MIN_TEST_ROWS = 1500
MIN_GAIN = 0.005      # candidate must be >= 0.5% better on average
MAX_LOSS = 0.010      # and no core stat more than 1% worse
CORE = ("pts", "reb", "ast", "fg3m")
PROP_MIN = backtest.PROP_MIN


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _mae(df: pl.DataFrame, s: str) -> float:
    return df.select((pl.col(f"proj_{s}") - pl.col(s)).abs().mean()).item()


def _score(box, targets, p) -> tuple[dict, pl.DataFrame]:
    df = backtest.run_projection(box, targets, p).filter(pl.col("proj_min") >= PROP_MIN)
    return {s: _mae(df, s) for s in STATS}, df


def tune(box: pl.DataFrame, targets: pl.DataFrame, start: model.Params) -> model.Params:
    """Coordinate search, same grids as the pre-launch backtest. Context
    on/off switches are left as they are (dropping one needs a stronger
    case than a month of games)."""
    def total(sc, stats=CORE):
        return sum(sc[s] for s in stats)

    best_k = {}
    k_scores = {}
    for kv in backtest.K_GRID:
        sc, _ = _score(box, targets, replace(start, k={s: kv for s in STATS}))
        for s in STATS:
            k_scores.setdefault(s, {})[kv] = sc[s]
    for s in STATS:
        best_k[s] = min(k_scores[s], key=k_scores[s].get)
    p = replace(start, k=best_k)
    for field, vals in (("min_half_life", (5.0, 8.0, 12.0)),
                        ("redistribute", (0.3, 0.5, 0.7, 1.0)),
                        ("usage_b", (0.0, 0.1, 0.2)),
                        ("a_def", (0.25, 0.5, 0.75)),
                        ("team_share_alpha", (0.25, 0.5, 0.75))):
        scores = {v: total(_score(box, targets, replace(p, **{field: v}))[0]) for v in vals}
        p = replace(p, **{field: min(scores, key=scores.get)})
    return p


def _params_dict(p: model.Params) -> dict:
    d = asdict(p)
    d["season_w"] = list(d["season_w"])
    return d


def main(dry_run: bool = False) -> dict:
    t0 = time.time()
    season = dataio.CURRENT_SEASON
    box = dataio.load_box()
    fit = live.load_fit()
    current = fit["params"]
    entry = {"at": _now(), "season": season, "adopted": False}

    cur_t = backtest.targets_for(box, season) if box.height else pl.DataFrame()
    if not cur_t.height:
        entry["result"] = "skipped: no box scores from this season yet"
        return _log(entry)
    dates = sorted(cur_t["date"].unique().to_list())
    cut = dates[int(len(dates) * (1 - TEST_SHARE))] if len(dates) >= 10 else None
    if cut is None:
        entry["result"] = f"skipped: only {len(dates)} game days this season so far"
        return _log(entry)
    tune_cur = cur_t.filter(pl.col("date") < cut)
    test_t = cur_t.filter(pl.col("date") >= cut)

    cur_sc, cur_df = _score(box, test_t, current)
    n_test = cur_df.height
    entry.update(test_from=str(cut), test_rows=n_test)
    if n_test < MIN_TEST_ROWS:
        entry["result"] = f"skipped: {n_test}/{MIN_TEST_ROWS} held-out player-games so far"
        return _log(entry)

    tune_t = pl.concat([backtest.targets_for(box, season - 1), tune_cur], how="vertical_relaxed")
    cand = tune(box, tune_t, current)
    cand_sc, _ = _score(box, test_t, cand)

    ratios = {s: cand_sc[s] / cur_sc[s] for s in CORE}
    avg_gain = 1 - sum(ratios.values()) / len(ratios)
    worst = max(ratios.values()) - 1
    entry.update(
        current_mae={s: round(cur_sc[s], 4) for s in STATS},
        candidate_mae={s: round(cand_sc[s], 4) for s in STATS},
        avg_gain_pct=round(100 * avg_gain, 2), worst_stat_change_pct=round(100 * worst, 2),
        changes={k: {"from": v, "to": getattr(cand, k)} for k, v in _params_dict(current).items()
                 if _params_dict(cand)[k] != v and k != "season_w"},
        runtime_s=round(time.time() - t0, 1),
    )
    if not entry["changes"]:
        entry["result"] = "kept: the search landed on the current settings"
        return _log(entry)
    if avg_gain < MIN_GAIN or worst > MAX_LOSS:
        entry["result"] = (f"kept current: candidate was {100 * avg_gain:+.2f}% on held-out games "
                           f"(needs +{100 * MIN_GAIN:.1f}%, worst stat {100 * worst:+.2f}%, limit +{100 * MAX_LOSS:.0f}%)")
        return _log(entry)

    entry["result"] = f"adopted: {100 * avg_gain:+.2f}% lower average miss on held-out games"
    if dry_run:
        entry["result"] += " (dry run: not saved)"
        return _log(entry, save=False)
    # refit the spread of outcomes with the new settings, on the tune set
    _, tune_df = _score(box, tune_t, cand)
    bt = json.loads(BT_PATH.read_text())
    entry["previous_params"] = bt.get("params")
    entry["previous_variance_fit"] = bt.get("variance_fit")
    bt["params"] = _params_dict(cand)
    bt["best_k"] = cand.k
    bt["variance_fit"] = {s: model.fit_variance(tune_df, s) for s in STATS}
    bt["last_retune"] = {k: entry[k] for k in ("at", "result", "test_from", "test_rows", "avg_gain_pct")}
    BT_PATH.write_text(json.dumps(bt, indent=2, default=str))
    entry["adopted"] = True
    return _log(entry)


def _log(entry: dict, save: bool = True) -> dict:
    print(json.dumps({k: v for k, v in entry.items() if not k.startswith("previous_")}, indent=1, default=str))
    if save:
        try:
            log = json.loads(LOG_PATH.read_text())
        except Exception:  # noqa: BLE001
            log = []
        LOG_PATH.write_text(json.dumps((log + [entry])[-36:], indent=1, default=str))
    return entry


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    main(ap.parse_args().dry_run)
