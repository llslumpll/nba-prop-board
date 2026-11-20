"""Five positions (update 86, Nov 20 2026): PG / SG / SF / PF / C for every player.

ESPN's box scores and rosters only say G, F or C, so "what a defense allows to guards" mixed
point guards with shooting guards (and small forwards with power forwards). This splits them:

  1. Official: NBA.com's daily starting lineups list a position for each starter. Every time a
     player starts, lineups_client records it in data/positions.json ("official": counts per
     position); his most frequent listed position wins (needs 2+ listings).
  2. Otherwise, from how he plays (all seasons in the box, minutes-weighted):
       C -> C
       G -> PG if his assists per 36 are at or above the league's minutes-weighted median for
            guards, else SG
       F -> PF if his (rebounds per 36 - 0.5 x threes attempted per 36) is at or above the
            forwards' median, else SF (stretch bigs who live at the arc read as SF-like)
     Players with under 100 total minutes keep a default (G -> SG, F -> SF, C -> C).

The model uses `dpos` only for "what this defense allows to this position"; per-minute position
priors still use the G/F/C group (steadier samples).
"""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import polars as pl

PATH = Path(__file__).resolve().parent.parent / "data" / "positions.json"
FIVE = ("PG", "SG", "SF", "PF", "C")
NAMES = {"PG": "point guards", "SG": "shooting guards", "SF": "small forwards", "PF": "power forwards", "C": "centers"}
DEFAULT = {"G": "SG", "F": "SF", "C": "C"}
MIN_MINUTES = 100.0
MIN_OFFICIAL = 2


def _load() -> dict:
    try:
        return json.loads(PATH.read_text())
    except Exception:  # noqa: BLE001
        return {"official": {}}


def record_official(listed: list[dict]) -> int:
    """listed: [{"player_id": int, "pos": "PG"|...}] from today's NBA.com starting lineups.
    Adds one listing per player per date (date-keyed so re-pulls don't double count)."""
    d = _load()
    off = d.setdefault("official", {})
    added = 0
    for x in listed:
        pid, pos, date = str(x.get("player_id") or ""), str(x.get("pos") or "").upper(), x.get("date")
        if not pid or pos not in FIVE or not date:
            continue
        rec = off.setdefault(pid, {"dates": {}, "player": x.get("player")})
        if rec["dates"].get(date) != pos:
            rec["dates"][date] = pos
            added += 1
    if added:
        PATH.write_text(json.dumps(d, indent=1, sort_keys=True))
    return added


def _official() -> dict[int, str]:
    out = {}
    for pid, rec in (_load().get("official") or {}).items():
        c = Counter((rec.get("dates") or {}).values())
        if c:
            pos, n = c.most_common(1)[0]
            if n >= MIN_OFFICIAL:
                out[int(pid)] = pos
    return out


def _wmedian(vals: list[float], wts: list[float]) -> float:
    pairs = sorted(zip(vals, wts))
    half, acc = sum(wts) / 2, 0.0
    for v, w in pairs:
        acc += w
        if acc >= half:
            return v
    return pairs[-1][0] if pairs else 0.0


def assign(box: pl.DataFrame) -> pl.DataFrame:
    """player_id -> dpos for every player in the box (one row each)."""
    if not box.height:
        return pl.DataFrame({"player_id": [], "dpos": []}, schema={"player_id": pl.Int64, "dpos": pl.Utf8})
    played = box.filter(pl.col("played") & (pl.col("min") > 0))
    grp = (box.sort("date").group_by("player_id").agg(pl.col("pos").last().alias("grp")))
    agg = played.group_by("player_id").agg(
        pl.col("min").sum().alias("m"), pl.col("ast").sum(), pl.col("reb").sum(), pl.col("fg3a").sum())
    t = grp.join(agg, on="player_id", how="left").with_columns(pl.col("m").fill_null(0.0))
    t = t.with_columns(
        (pl.col("ast") / pl.col("m") * 36).alias("ast36"),
        ((pl.col("reb") - 0.5 * pl.col("fg3a")) / pl.col("m") * 36).alias("big36"))
    ok = t.filter(pl.col("m") >= MIN_MINUTES)
    g = ok.filter(pl.col("grp") == "G")
    f = ok.filter(pl.col("grp") == "F")
    g_cut = _wmedian(g["ast36"].to_list(), g["m"].to_list()) if g.height else 99.0
    f_cut = _wmedian(f["big36"].to_list(), f["m"].to_list()) if f.height else 99.0
    off = _official()
    rows = []
    for r in t.iter_rows(named=True):
        pid, gp = r["player_id"], r["grp"] or "F"
        if pid in off:
            pos = off[pid]
        elif gp == "C":
            pos = "C"
        elif r["m"] < MIN_MINUTES:
            pos = DEFAULT.get(gp, "SF")
        elif gp == "G":
            pos = "PG" if (r["ast36"] or 0) >= g_cut else "SG"
        else:
            pos = "PF" if (r["big36"] or 0) >= f_cut else "SF"
        rows.append((pid, pos))
    return pl.DataFrame(rows, schema={"player_id": pl.Int64, "dpos": pl.Utf8}, orient="row")
