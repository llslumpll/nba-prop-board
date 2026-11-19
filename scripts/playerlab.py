"""Player Lab (update 81): the interactive section on each player page.

All from data the site already downloads for free:
- game log with filters (last N, home/away, vs tonight's opponent, back-to-back, won/lost by 10+, minutes)
  from ESPN box scores, plus the line we graded each game against (data/predictions/*.json);
- shooting zones (rim / paint / midrange / corner three / above-the-break three) and shot types from ESPN
  play-by-play (sportsdataverse): every shot has court coordinates and ESPN's shot label;
- opponent defense: points each team allows per game in each zone / shot type, ranked 1 = allows the most;
- similar players vs tonight's opponent: same position, the line we graded them on, and what they did.

Shot types are our estimates from ESPN's shot labels (catch-and-shoot = assisted jumper, pull-up = pull-up /
step-back / fadeaway / unassisted jumper, drives = driving / running / floating shots, cuts & lobs, post & hooks,
putbacks & tips). Real "play types" (pick-and-roll, spot-up, transition) are licensed tracking data and are not
published for free; the page says so.
"""
from __future__ import annotations

import json
import math
from collections import defaultdict
from datetime import date as _date, timedelta
from pathlib import Path

import polars as pl

ROOT = Path(__file__).resolve().parent.parent
PRED_DIR = ROOT / "data" / "predictions"
STATS = ("pts", "reb", "ast", "fg3m", "pra", "pa", "pr", "ra")
ZONES = ("rim", "paint", "mid", "corner3", "above3")
ZONE_LABEL = {"rim": "At the rim", "paint": "Paint (non-rim)", "mid": "Midrange", "corner3": "Corner 3", "above3": "Above-break 3"}
TYPES = ("cns", "pullup", "drive", "cut", "post", "putback", "ft")
TYPE_LABEL = {"cns": "Catch & shoot", "pullup": "Pull-up / self-created", "drive": "Drives", "cut": "Cuts & lobs",
              "post": "Post & hooks", "putback": "Putbacks & tips", "ft": "Free throws"}


# ------------------------------------------------------------------ play-by-play -> zones and shot types
def classify(pbp: pl.DataFrame) -> pl.DataFrame:
    """One row per field-goal attempt or made free throw: game_id, date, player_id, team_id, def_team_id,
    zone, kind, pts (points scored on the play), fga (1 for shots)."""
    fg = pbp.filter(pl.col("shooting_play") & pl.col("athlete_id_1").is_not_null())
    tt = pl.col("type_text").fill_null("")
    is_ft = tt.str.contains("Free Throw")
    ax = pl.col("coordinate_x").abs()
    ay = pl.col("coordinate_y").abs()
    dist = ((ax - 41.75) ** 2 + pl.col("coordinate_y") ** 2).sqrt()
    three = pl.col("points_attempted") == 3
    zone = (pl.when(is_ft).then(pl.lit("ft"))
            .when(three & (ay >= 21.5) & (ax >= 33)).then(pl.lit("corner3"))
            .when(three).then(pl.lit("above3"))
            .when(dist <= 4.5).then(pl.lit("rim"))
            .when((ax >= 28) & (ay <= 8)).then(pl.lit("paint"))
            .otherwise(pl.lit("mid")))
    assisted = pl.col("athlete_id_2").is_not_null()
    kind = (pl.when(is_ft).then(pl.lit("ft"))
            .when(tt.str.contains("(?i)tip|putback")).then(pl.lit("putback"))
            .when(tt.str.contains("(?i)alley|cutting")).then(pl.lit("cut"))
            .when(tt.str.contains("(?i)hook|post|turnaround")).then(pl.lit("post"))
            .when(tt.str.contains("(?i)driving|running|floating|finger")).then(pl.lit("drive"))
            .when(tt.str.contains("(?i)pullup|pull-up|step back|stepback|fade")).then(pl.lit("pullup"))
            .when(tt.str.contains("(?i)jump") & assisted).then(pl.lit("cns"))
            .when(tt.str.contains("(?i)jump")).then(pl.lit("pullup"))
            .when(tt.str.contains("(?i)layup|dunk")).then(pl.when(assisted).then(pl.lit("cut")).otherwise(pl.lit("drive")))
            .otherwise(pl.lit("pullup")))
    out = fg.with_columns(
        zone.alias("zone"), kind.alias("kind"),
        pl.when(pl.col("scoring_play")).then(pl.col("score_value")).otherwise(0).fill_null(0).alias("pts"),
        pl.when(is_ft).then(0).otherwise(1).alias("fga"),
        pl.when(pl.col("team_id") == pl.col("home_team_id")).then(pl.col("away_team_id")).otherwise(pl.col("home_team_id")).alias("def_team_id"),
    )
    return out.select(pl.col("game_id"), pl.col("game_date").alias("date"), pl.col("athlete_id_1").cast(pl.Int64).alias("player_id"),
                      pl.col("team_id").cast(pl.Int64), pl.col("def_team_id").cast(pl.Int64), "zone", "kind", "pts", "fga")


def defense_ranks(shots: pl.DataFrame, team_code: dict) -> dict:
    """{team_code: {"zone": {z: (ppg_allowed, rank)}, "kind": {k: (ppg, rank)}}}; rank 1 = allows the most."""
    games = shots.group_by("def_team_id").agg(pl.col("game_id").n_unique().alias("g"))
    out: dict = defaultdict(lambda: {"zone": {}, "kind": {}})
    for col, keys in (("zone", ZONES + ("ft",)), ("kind", TYPES)):
        agg = (shots.group_by("def_team_id", col).agg(pl.col("pts").sum().alias("p"))
               .join(games, on="def_team_id").with_columns((pl.col("p") / pl.col("g")).alias("ppg")))
        for k in keys:
            rows = agg.filter(pl.col(col) == k).sort("ppg", descending=True).rows(named=True)
            for i, r in enumerate(rows, 1):
                code = team_code.get(r["def_team_id"])
                if code:
                    out[code][col][k] = (round(r["ppg"], 1), i)
    return dict(out)


def player_shots(shots: pl.DataFrame, player_id: int) -> dict:
    me = shots.filter(pl.col("player_id") == player_id)
    games = max(1, me["game_id"].n_unique())
    tot = max(1, int(me["pts"].sum()))
    zones = {}
    for z in ZONES:
        s = me.filter(pl.col("zone") == z)
        att, mk = int(s["fga"].sum()), int((s["pts"] > 0).sum())
        zones[z] = {"pts": int(s["pts"].sum()), "share": round(100 * s["pts"].sum() / tot), "fga": att,
                    "fg": round(100 * mk / att) if att else None, "ppg": round(s["pts"].sum() / games, 1)}
    kinds = {}
    for k in TYPES:
        s = me.filter(pl.col("kind") == k)
        kinds[k] = {"ppg": round(s["pts"].sum() / games, 1), "share": round(100 * s["pts"].sum() / tot)}
    return {"games": games, "zones": zones, "kinds": kinds}


# ------------------------------------------------------------------ game log + lines
def _lines_index(dates: list[str]) -> dict:
    """{(player_id, date, stat): (line, be_over)} from saved predictions."""
    idx = {}
    for d in dates:
        try:
            rows = json.loads((PRED_DIR / f"{d}.json").read_text())
        except Exception:  # noqa: BLE001
            continue
        for r in rows.values():
            if r.get("line") is not None and r.get("player_id") is not None:
                idx[(int(r["player_id"]), d, r["stat"])] = (r["line"], r.get("be_over"))
    return idx


def gamelog(raw_box: pl.DataFrame, player_id: int, lines: dict) -> list[dict]:
    me = (raw_box.filter((pl.col("athlete_id") == player_id) & ~pl.col("did_not_play").fill_null(False)
                         & (pl.col("minutes").fill_null(0) > 0)).sort("game_date"))
    rows, prev = [], None
    for r in me.iter_rows(named=True):
        d = r["game_date"]
        p, rb, a, t = r["points"] or 0, r["rebounds"] or 0, r["assists"] or 0, r["three_point_field_goals_made"] or 0
        fga, fta, tov, mn = r["field_goals_attempted"] or 0, r["free_throws_attempted"] or 0, r["turnovers"] or 0, r["minutes"] or 0
        vals = {"pts": p, "reb": rb, "ast": a, "fg3m": t, "pra": p + rb + a, "pa": p + a, "pr": p + rb, "ra": rb + a}
        ds = d.isoformat()
        rows.append({"d": ds, "opp": r["opponent_team_abbreviation"], "home": r["home_away"] == "home",
                     "b2b": prev is not None and (d - prev) == timedelta(days=1),
                     "m": (r["team_score"] or 0) - (r["opponent_team_score"] or 0), "min": round(mn),
                     "fga": fga, "fg3a": r["three_point_field_goals_attempted"] or 0, "fta": fta,
                     "usg": round(36 * (fga + 0.44 * fta + tov) / mn, 1) if mn else 0,
                     "v": vals, "ln": {s: (lines.get((player_id, ds, s)) or (None,))[0] for s in STATS}})
        prev = d
    return rows


def similar(raw_box: pl.DataFrame, lines: dict, pos: str, opp_raw: str, exclude: int, season_start: _date) -> list[dict]:
    """Same-position players vs this opponent this season, with the line we graded them on."""
    vs = raw_box.filter((pl.col("opponent_team_abbreviation") == opp_raw) & (pl.col("athlete_position_abbreviation") == pos)
                        & (pl.col("game_date") >= season_start) & (pl.col("athlete_id") != exclude)
                        & (pl.col("minutes").fill_null(0) >= 15)).sort("game_date", descending=True)
    out = []
    for r in vs.iter_rows(named=True):
        ds = r["game_date"].isoformat()
        p, rb, a, t = r["points"] or 0, r["rebounds"] or 0, r["assists"] or 0, r["three_point_field_goals_made"] or 0
        vals = {"pts": p, "reb": rb, "ast": a, "fg3m": t, "pra": p + rb + a, "pa": p + a, "pr": p + rb, "ra": rb + a}
        ln = {s: (lines.get((int(r["athlete_id"]), ds, s)) or (None,))[0] for s in STATS}
        if not any(v is not None for v in ln.values()):
            continue
        out.append({"d": ds, "team": r["team_abbreviation"], "name": r["athlete_display_name"], "v": vals, "ln": ln})
    return out


# ------------------------------------------------------------------ one player's payload
def payload(raw_box, shots, ranks, team_code, player_id: int, opp: str, opp_raw: str, tonight: dict) -> dict:
    lines = _lines_index(sorted({d.isoformat() for d in raw_box["game_date"].unique().to_list()}))
    log = gamelog(raw_box, player_id, lines)
    me = raw_box.filter(pl.col("athlete_id") == player_id).sort("game_date").tail(1)
    pos = me["athlete_position_abbreviation"][0] if me.height else None
    start = _date(2026, 10, 1)
    return {"log": log, "tonight": tonight, "opp": opp, "pos": pos,
            "shots": player_shots(shots, player_id) if shots is not None else None,
            "def": ranks.get(opp) if ranks else None, "nteams": len(ranks or {}),
            "similar": similar(raw_box, lines, pos, opp_raw, player_id, start) if pos else [],
            "zone_label": ZONE_LABEL, "type_label": TYPE_LABEL}
