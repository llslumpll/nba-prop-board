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
        pl.when(pl.col("scoring_play") & ~is_ft).then(pl.col("athlete_id_2")).otherwise(None).cast(pl.Int64).alias("ast_id"),
        pl.when(pl.col("scoring_play")).then(pl.col("score_value")).otherwise(0).fill_null(0).alias("pts"),
        pl.when(is_ft).then(0).otherwise(1).alias("fga"),
        pl.when(pl.col("team_id") == pl.col("home_team_id")).then(pl.col("away_team_id")).otherwise(pl.col("home_team_id")).alias("def_team_id"),
    )
    return out.select(pl.col("game_id"), pl.col("game_date").alias("date"), pl.col("athlete_id_1").cast(pl.Int64).alias("player_id"),
                      pl.col("team_id").cast(pl.Int64), pl.col("def_team_id").cast(pl.Int64), "zone", "kind", "pts", "fga", "ast_id")


def rebounds(pbp: pl.DataFrame) -> pl.DataFrame:
    """Update 91: one row per player rebound: game_id, date, player_id, team_id, def_team_id (the team he faced),
    zone = where the missed shot came from (the same five zones, or ft), off = offensive rebound."""
    p = pbp.sort(["game_id", "sequence_number"])
    tt = pl.col("type_text").fill_null("")
    ax, ay = pl.col("coordinate_x").abs(), pl.col("coordinate_y").abs()
    dist = ((ax - 41.75) ** 2 + pl.col("coordinate_y") ** 2).sqrt()
    three = pl.col("points_attempted") == 3
    z = (pl.when(~pl.col("shooting_play")).then(None)
         .when(tt.str.contains("Free Throw")).then(pl.lit("ft"))
         .when(three & (ay >= 21.5) & (ax >= 33)).then(pl.lit("corner3"))
         .when(three).then(pl.lit("above3"))
         .when(dist <= 4.5).then(pl.lit("rim"))
         .when((ax >= 28) & (ay <= 8)).then(pl.lit("paint"))
         .otherwise(pl.lit("mid")))
    p = p.with_columns(z.alias("z5")).with_columns(pl.col("z5").shift(1).over("game_id").alias("zone"))
    rb = p.filter(tt.str.contains("Rebound") & pl.col("athlete_id_1").is_not_null() & pl.col("zone").is_not_null())
    rb = rb.with_columns(
        pl.when(pl.col("team_id") == pl.col("home_team_id")).then(pl.col("away_team_id")).otherwise(pl.col("home_team_id")).alias("def_team_id"),
        (pl.col("type_text") == "Offensive Rebound").alias("off"))
    return rb.select(pl.col("game_id"), pl.col("game_date").alias("date"), pl.col("athlete_id_1").cast(pl.Int64).alias("player_id"),
                     pl.col("team_id").cast(pl.Int64), pl.col("def_team_id").cast(pl.Int64), "zone", "off")


def _rank(df: pl.DataFrame, games: pl.DataFrame, val: str, team_code: dict, keys) -> dict:
    """{team_code: {zone: (per game, rank)}}, rank 1 = allows the most."""
    out: dict = defaultdict(dict)
    agg = (df.group_by("def_team_id", "zone").agg(pl.col(val).sum().alias("p"))
           .join(games, on="def_team_id").with_columns((pl.col("p") / pl.col("g")).alias("pg")))
    for k in keys:
        rows = agg.filter(pl.col("zone") == k).sort("pg", descending=True).rows(named=True)
        for i, r in enumerate(rows, 1):
            code = team_code.get(r["def_team_id"])
            if code:
                out[code][k] = (round(r["pg"], 1), i)
    return dict(out)


def stat_ranks(shots: pl.DataFrame, rebs: pl.DataFrame | None, team_code: dict) -> dict:
    """Update 91: zone ranks for the other stats. {"ast": assists allowed by the zone of the shot set up,
    "fg3m": threes made allowed (corner / above-break), "fg3a": threes attempted, "reb": rebounds allowed off misses
    from each zone, "oreb" / "dreb": per team {"all": (pg, rank)}}."""
    games = shots.group_by("def_team_id").agg(pl.col("game_id").n_unique().alias("g"))
    out = {}
    if "ast_id" in shots.columns:
        a = shots.filter(pl.col("ast_id").is_not_null()).with_columns(pl.lit(1).alias("one"))
        out["ast"] = _rank(a, games, "one", team_code, ZONES)
    t = shots.filter(pl.col("zone").is_in(["corner3", "above3"])).with_columns(
        (pl.col("pts") > 0).cast(pl.Int32).alias("made"), pl.col("fga").alias("att"))
    out["fg3m"] = _rank(t, games, "made", team_code, ("corner3", "above3"))
    out["fg3a"] = _rank(t, games, "att", team_code, ("corner3", "above3"))
    if rebs is not None and rebs.height:
        r = rebs.with_columns(pl.lit(1).alias("one"))
        out["reb"] = _rank(r, games, "one", team_code, ZONES + ("ft",))
        for nm, flt in (("oreb", pl.col("off")), ("dreb", ~pl.col("off"))):
            out[nm] = _rank(r.filter(flt).with_columns(pl.lit("all").alias("zone")), games, "one", team_code, ("all",))
    return out


def player_other(shots: pl.DataFrame, rebs: pl.DataFrame | None, player_id: int) -> dict:
    """His assists by the zone of the shot he set up, his rebounds by where the miss came from, his threes by zone."""
    out = {}
    if "ast_id" in shots.columns:
        a = shots.filter(pl.col("ast_id") == player_id)
        tot = max(1, a.height)
        g = max(1, shots.filter((pl.col("player_id") == player_id) | (pl.col("ast_id") == player_id))["game_id"].n_unique())
        out["ast"] = {"n": a.height, "zones": {z: {"share": round(100 * a.filter(pl.col("zone") == z).height / tot),
                                                    "pg": round(a.filter(pl.col("zone") == z).height / g, 1)} for z in ZONES}}
    me = shots.filter((pl.col("player_id") == player_id) & pl.col("zone").is_in(["corner3", "above3"]))
    att = max(1, int(me["fga"].sum()))
    g = max(1, shots.filter(pl.col("player_id") == player_id)["game_id"].n_unique())
    out["fg3m"] = {"n": int(me["fga"].sum()), "zones": {}}
    for z in ("corner3", "above3"):
        s = me.filter(pl.col("zone") == z)
        za, zm = int(s["fga"].sum()), int((s["pts"] > 0).sum())
        out["fg3m"]["zones"][z] = {"share": round(100 * za / att), "fg": round(100 * zm / za) if za else None,
                                   "pg": round(zm / g, 1), "apg": round(za / g, 1)}
    if rebs is not None and rebs.height:
        r = rebs.filter(pl.col("player_id") == player_id)
        tot = max(1, r.height)
        g = max(1, r["game_id"].n_unique())
        out["reb"] = {"n": r.height, "off": round(100 * r["off"].sum() / tot),
                      "zones": {z: {"share": round(100 * r.filter(pl.col("zone") == z).height / tot),
                                    "pg": round(r.filter(pl.col("zone") == z).height / g, 1)} for z in ZONES + ("ft",)}}
    return out


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


# ------------------------------------------------------------------ cache + build context
CACHE = ROOT / "data" / "cache"


def shots_path(season: int) -> Path:
    return CACHE / f"shots_{season}.parquet"


def reb_path(season: int) -> Path:
    return CACHE / f"rebounds_{season}.parquet"


def save_shots(pbp: pl.DataFrame, season: int) -> int:
    sh = classify(pbp)
    tmp = shots_path(season).with_suffix(".tmp")
    sh.write_parquet(tmp)
    tmp.replace(shots_path(season))
    try:
        rb = rebounds(pbp)
        tmp = reb_path(season).with_suffix(".tmp")
        rb.write_parquet(tmp)
        tmp.replace(reb_path(season))
    except Exception as e:  # noqa: BLE001
        print(f"  player lab rebounds: {e}")
    return sh.height


class Ctx:
    """Everything shared by all player pages in one build (loaded once)."""

    def __init__(self, season: int):
        import teams
        self.raw = pl.read_parquet(CACHE / f"player_box_{season}.parquet").filter(pl.col("season_type") == 2) \
            if "season_type" in pl.read_parquet_schema(CACHE / f"player_box_{season}.parquet") else pl.read_parquet(CACHE / f"player_box_{season}.parquet")
        self.team_code = {r[0]: r[1] for r in self.raw.select("team_id", "team_abbreviation").unique().iter_rows()}
        self.to_raw = {v: k for k, v in teams.ALIASES["espn"].items()}
        self.shots = pl.read_parquet(shots_path(season)) if shots_path(season).exists() else None
        self.ranks = defense_ranks(self.shots, self.team_code) if self.shots is not None else {}
        self.rebs = pl.read_parquet(reb_path(season)) if reb_path(season).exists() else None
        try:
            self.sranks = stat_ranks(self.shots, self.rebs, self.team_code) if self.shots is not None else {}
        except Exception as e:  # noqa: BLE001
            print(f"  player lab stat ranks: {e}")
            self.sranks = {}
        self.lines = _lines_index(sorted({d.isoformat() for d in self.raw["game_date"].unique().to_list()}))
        try:
            self.track = track_record(self.raw, self.shots, self.team_code, self.to_raw, self.lines)
        except Exception as e:  # noqa: BLE001
            print(f"  player lab track record: {e}")
            self.track = None

    def for_player(self, player_id: int, opp: str, preds: list) -> dict | None:
        try:
            tonight = {p["stat"]: {"line": p.get("line"), "be_over": p.get("be_over"), "call": p.get("call"),
                                   "proj": p.get("projected")} for p in preds if p.get("line") is not None}
            raw_opp = self.to_raw.get(opp, opp)
            d = payload(self.raw, self.shots, self.ranks, self.team_code, int(player_id), raw_opp, raw_opp, tonight, self.lines)
            d["opp"], d["opp_raw"] = opp, raw_opp
            d["def"] = self.ranks.get(raw_opp)
            if self.shots is not None:  # update 91: the matchup panel for assists, rebounds and threes too
                d["other"] = player_other(self.shots, self.rebs, int(player_id))
                d["odef"] = {k: v.get(raw_opp) for k, v in self.sranks.items()}
            d["first"] = next((s for s in ("pts", "fg3m", "reb", "ast") if s in tonight), "pts")
            d["track"] = self.track
            return d if d["log"] else None
        except Exception as e:  # noqa: BLE001
            print(f"  player lab {player_id}: {e}")
            return None


# ------------------------------------------------------------------ one player's payload
def payload(raw_box, shots, ranks, team_code, player_id: int, opp: str, opp_raw: str, tonight: dict, lines=None) -> dict:
    if lines is None:
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


# ------------------------------------------------------------------ the 4 signals (same rules as playerlab.js)
SCORING = ("pts", "fg3m", "pra", "pa", "pr")


def signals(log: list, stat: str, line, be_over, call, shots, def_, nteams: int, sim: list, n: int = 15) -> dict:
    """What the verdict box shows by default (last 15 games, no filters). Returns
    {"side", "sig": {name: "g"|"r"|"n"}, "green", "red", "tone"}; tone good = 3+ green and no red, bad = 2+ red."""
    side = "UNDER" if call == "UNDER" else "OVER"
    sgn = 1 if side == "OVER" else -1
    r = log[-n:] if n else log
    sig = {}
    if line is not None and be_over is not None and r:
        rate = sum(1 for g in r if g["v"][stat] > line) / len(r)
        d = (rate - be_over) * sgn
        sig["form"] = "g" if d > .05 else ("r" if d < -.05 else "n")
    else:
        sig["form"] = "n"
    if stat in SCORING and shots and def_:
        N = nteams or 30
        sc = w = 0.0
        for k in TYPE_LABEL:
            sh = shots["kinds"][k]["share"] / 100
            rk = (def_.get("kind", {}).get(k) or (0, 15))[1]
            sc += sh * (N + 1 - rk) / N
            w += sh
        sc = sc / w if w else .5
        if sgn > 0:
            sig["matchup"] = "g" if sc > .55 else ("r" if sc < .45 else "n")
        else:
            sig["matchup"] = "g" if sc < .45 else ("r" if sc > .55 else "n")
    else:
        sig["matchup"] = "n"
    sr = [g for g in sim if g["ln"].get(stat) is not None]
    sr = sr[:n] if n else sr
    if len(sr) >= 4:
        so = (sum(1 for g in sr if g["v"][stat] > g["ln"][stat]) / len(sr) - .5) * sgn
        sig["similar"] = "g" if so > .08 else ("r" if so < -.08 else "n")
    else:
        sig["similar"] = "n"
    mins = [g["min"] for g in log]
    if mins:
        l5, sz = sum(mins[-5:]) / len(mins[-5:]), sum(mins) / len(mins)
        sig["minutes"] = "g" if l5 - sz > 2 else ("r" if l5 - sz < -2 else "n")
    g = sum(v == "g" for v in sig.values())
    rd = sum(v == "r" for v in sig.values())
    return {"side": side, "sig": sig, "green": g, "red": rd,
            "tone": "good" if g >= 3 and rd == 0 else ("bad" if rd >= 2 else "mixed")}


# ------------------------------------------------------------------ track record of the signals (Nov 19)
TRACK_DIR = ROOT / "data" / "labsignals"
SIG_KEYS = ("form", "matchup", "similar", "minutes")
# saved row: [stat, side O/U, closing price, hit 0/1, tone g/m/b, lights "gnrn" in SIG_KEYS order]


def _rec(rs: list) -> dict:
    n = len(rs)
    if not n:
        return {"n": 0}
    w = sum(r["hit"] for r in rs)
    u = sum((1 / r["price"] - 1) if r["hit"] else -1 for r in rs)
    return {"n": n, "w": w, "l": n - w, "rate": round(100 * w / n, 1),
            "price": round(100 * sum(r["price"] for r in rs) / n, 1), "u": round(u, 1), "roi": round(100 * u / n, 1)}


def day_signals(raw, shots_all, team_code, to_raw, lines, ds: str) -> list:
    """Every graded line on date ds with the signals the page would have shown that day: only games before ds
    are used (game log, shot profile, defense ranks, similar players). The price is the closing break-even."""
    d0 = _date.fromisoformat(ds)
    try:
        preds = json.loads((PRED_DIR / f"{ds}.json").read_text())
    except Exception:  # noqa: BLE001
        return []
    graded = [p for p in preds.values() if p.get("hit") is not None and p.get("line") is not None and p.get("player_id")]
    rb = raw.filter(pl.col("game_date") < d0)
    sh = shots_all.filter(pl.col("date") < d0) if shots_all is not None else None
    if not graded or not rb.height or sh is None or not sh.height:
        return []
    ranks = defense_ranks(sh, team_code)
    have = set(sh["player_id"].unique().to_list())
    cache, out = {}, []
    for p in graded:
        pid, opp = int(p["player_id"]), to_raw.get(p.get("opp"), p.get("opp"))
        if (pid, opp) not in cache:
            log = gamelog(rb, pid, lines)
            me = rb.filter(pl.col("athlete_id") == pid).sort("game_date").tail(1)
            pos = me["athlete_position_abbreviation"][0] if me.height else None
            cache[(pid, opp)] = (log, player_shots(sh, pid) if pid in have else None,
                                 similar(rb, lines, pos, opp, pid, _date(2026, 10, 1)) if pos else [])
        log, ps, sim = cache[(pid, opp)]
        if not log:
            continue
        s = signals(log, p["stat"], p["line"], p.get("be_over"), p.get("call"), ps, ranks.get(opp), len(ranks), sim)
        price = p.get("be_over") if s["side"] == "OVER" else p.get("be_under")
        if not price or not 0 < price < 1:
            continue
        sg = s["sig"]
        out.append([p["stat"], s["side"][0], round(price, 4), int(bool(p["hit"])), s["tone"][0],
                    "".join(sg.get(k, "n") for k in SIG_KEYS)])
    return out


def track_record(raw, shots_all, team_code, to_raw, lines) -> dict:
    """Season record of each verdict and each signal light. Days 2+ days old are saved to data/labsignals/ and
    reused; newer days are recomputed every build (late grades)."""
    TRACK_DIR.mkdir(parents=True, exist_ok=True)
    cutoff = (_date.today() - timedelta(days=2)).isoformat()
    rows, days = [], 0
    for f in sorted(PRED_DIR.glob("*.json")):
        ds = f.stem
        cf = TRACK_DIR / f"{ds}.json"
        day = None
        if cf.exists():
            try:
                day = json.loads(cf.read_text())
            except Exception:  # noqa: BLE001
                day = None
        if day is None:
            day = day_signals(raw, shots_all, team_code, to_raw, lines, ds)
            if day and ds <= cutoff:
                cf.write_text(json.dumps(day, separators=(",", ":")))
        if day:
            days += 1
            rows += [{"price": r[2], "hit": r[3], "tone": {"g": "good", "m": "mixed", "b": "bad"}[r[4]],
                      "sig": dict(zip(SIG_KEYS, r[5]))} for r in day]
    if not rows:
        return None
    return {"days": days, "from": min(f.stem for f in TRACK_DIR.glob("*.json")) if any(TRACK_DIR.glob("*.json")) else None,
            "tone": {t: _rec([r for r in rows if r["tone"] == t]) for t in ("good", "mixed", "bad")},
            "sig": {k: {v: _rec([r for r in rows if r["sig"].get(k) == v]) for v in "gr"}
                    for k in SIG_KEYS},
            "all": _rec(rows)}
