"""
Shot-zone matchups (update 59), from sportsdataverse's free ESPN play-by-play.

Every made / missed field goal is put in a zone: RIM (layups, dunks, tips, alley-oops, putbacks, finger rolls),
THREE, or MID (every other two). Kept per player-game: points scored by zone, and the defense he faced.
  data/cache/zones_{season}.parquet   game_id, date, season, player_id, def_team, rim, mid, three   (~250 KB/season)

The raw play-by-play (2-25 MB) is downloaded only when the upstream file changed (HEAD Last-Modified), into a temp
folder, summarized, and thrown away. Last season's summary is built once.

Used by model.py (zone_w): a defense that gives up more points than average in a zone, against a player who scores
a lot of his points there, nudges his points (and threes) projection. Tested Nov 4 on 2025-26 Dec-Mar (9,620
player-games, walk-forward, on top of the full model): points miss 4.763 -> 4.753, threes 0.995 -> 0.993 at 0.75.
Only once the defense has 15+ games this season (this season's first two weeks: no signal yet).
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import polars as pl
import requests

import dataio

PBP_URL = dataio.BASE + "/espn_nba_pbp/play_by_play_{season}.parquet"
ZONES = ("rim", "mid", "three")
RIM_RE = "(?i)layup|dunk|tip|alley|putback|finger"


def path(season: int) -> Path:
    return dataio.CACHE / f"zones_{season}.parquet"


def summarize(pbp: pl.DataFrame) -> pl.DataFrame:
    sh = pbp.filter(pl.col("shooting_play") & pl.col("points_attempted").is_in([2, 3])
                    & ~pl.col("type_text").str.contains("Free Throw") & pl.col("athlete_id_1").is_not_null())
    sh = sh.with_columns(
        zone=pl.when(pl.col("points_attempted") == 3).then(pl.lit("three"))
        .when(pl.col("type_text").str.contains(RIM_RE)).then(pl.lit("rim")).otherwise(pl.lit("mid")),
        def_team=pl.when(pl.col("team_id") == pl.col("home_team_id")).then(pl.col("away_team_abbrev"))
        .otherwise(pl.col("home_team_abbrev")))
    out = sh.group_by(["game_id", "game_date", "season", "season_type", "athlete_id_1", "def_team"]).agg(
        [pl.col("score_value").filter(pl.col("zone") == z).sum().alias(z) for z in ZONES])
    return out.rename({"game_date": "date", "athlete_id_1": "player_id"}).with_columns(
        pl.col("game_id").cast(pl.Int64), pl.col("player_id").cast(pl.Int64),
        pl.col("def_team").replace({"GS": "GSW", "NO": "NOP", "NY": "NYK", "SA": "SAS", "UTAH": "UTA", "WSH": "WAS"}),
        *[pl.col(z).cast(pl.Int32) for z in ZONES]).sort(["date", "game_id"])


def refresh(seasons=None) -> dict:
    """Build / update the zone summaries. Current season: only when the upstream file changed."""
    cur = dataio.CURRENT_SEASON
    seasons = seasons or (cur - 1, cur)
    status_p = dataio.CACHE / "zones_status.json"
    try:
        st = json.loads(status_p.read_text())
    except Exception:  # noqa: BLE001
        st = {}
    out = {}
    for s in seasons:
        url = PBP_URL.format(season=s)
        if s != cur and path(s).exists():
            out[s] = "kept"
            continue
        try:
            lm = requests.head(url, timeout=30, allow_redirects=True).headers.get("Last-Modified")
            if path(s).exists() and lm and st.get(str(s)) == lm:
                out[s] = "unchanged"
                continue
            r = requests.get(url, timeout=180, allow_redirects=True)
            if r.status_code == 404:
                out[s] = "not published"
                continue
            r.raise_for_status()
            with tempfile.TemporaryDirectory() as td:
                f = Path(td) / "pbp.parquet"
                f.write_bytes(r.content)
                z = summarize(pl.read_parquet(f))
            if z.height:
                tmp = path(s).with_suffix(".tmp")
                z.write_parquet(tmp)
                tmp.replace(path(s))
                st[str(s)] = lm
                out[s] = f"updated ({z.height} player-games)"
        except Exception as e:  # noqa: BLE001
            out[s] = f"error: {str(e)[:120]}"
    status_p.write_text(json.dumps(st, indent=1))
    return out


_cache = None


def load() -> pl.DataFrame:
    global _cache
    if _cache is None:
        fr = [pl.read_parquet(path(s)) for s in (dataio.CURRENT_SEASON - 1, dataio.CURRENT_SEASON) if path(s).exists()]
        _cache = pl.concat(fr, how="vertical_relaxed") if fr else pl.DataFrame()
    return _cache


def factors(t: pl.DataFrame, min_g: int = 15, k: float = 10.0) -> pl.DataFrame:
    """For each target row (game_id, player_id, opp, date, season): the opponent's points allowed by zone vs the league
    (this season, games before the date, shrunk toward 1 with k games; null below min_g games) and the player's share
    of his points by zone (this season + last, games before the date). Returns game_id, player_id, z_sig, z_three."""
    z = load()
    if not z.height or not t.height:
        return pl.DataFrame(schema={"game_id": pl.Int64, "player_id": pl.Int64, "z_sig": pl.Float64, "z_three": pl.Float64})
    z = z.filter(pl.col("season_type") == 2) if "season_type" in z.columns else z
    # defense: points allowed by zone per game, cumulative before each game
    d = z.group_by(["game_id", "date", "season", "def_team"]).agg([pl.col(c).sum() for c in ZONES]).sort("date")
    lg = d.group_by(["season", "date"]).agg([pl.col(c).sum().alias(f"s_{c}") for c in ZONES] + [pl.len().alias("n")]).sort("date")
    lg = lg.with_columns([(pl.col(f"s_{c}").cum_sum().over("season") - pl.col(f"s_{c}")).alias(f"lp_{c}") for c in ZONES]
                         + [(pl.col("n").cum_sum().over("season") - pl.col("n")).alias("lpn")])
    d = d.with_columns([(pl.col(c).cum_sum().over(["season", "def_team"]) - pl.col(c)).alias(f"p_{c}") for c in ZONES]
                       + [(pl.int_range(0, pl.len()).over(["season", "def_team"])).alias("pgp")])
    d = d.join(lg.select(["season", "date", "lpn"] + [f"lp_{c}" for c in ZONES]), on=["season", "date"])
    d = d.with_columns([pl.when(pl.col("pgp") >= min_g).then(
        ((pl.col(f"p_{c}") / pl.col("pgp")) / (pl.col(f"lp_{c}") / pl.col("lpn").clip(1, None)) * pl.col("pgp") + k) / (pl.col("pgp") + k))
        .otherwise(None).alias(f"f_{c}") for c in ZONES])
    # the opponent's latest state before each target date (as-of join on date)
    dd = d.select(pl.col("def_team").alias("opp"), pl.col("date").alias("d_date"), "season", "pgp",
                  *[pl.col(c).alias(f"g_{c}") for c in ZONES], *[f"p_{c}" for c in ZONES], "lpn", *[f"lp_{c}" for c in ZONES])
    # include the last game itself in "before the next date"
    dd = dd.with_columns([(pl.col(f"p_{c}") + pl.col(f"g_{c}")).alias(f"q_{c}") for c in ZONES]
                         + [(pl.col("pgp") + 1).alias("n_after")])
    tt = t.select(pl.col("game_id").cast(pl.Int64), pl.col("player_id").cast(pl.Int64), pl.col("opp").cast(pl.Utf8),
                  pl.col("date").cast(pl.Date), pl.col("season").cast(pl.Int64)).sort("date")
    dd = dd.with_columns(pl.col("d_date").cast(pl.Date), pl.col("season").cast(pl.Int64), pl.col("opp").cast(pl.Utf8)).sort("d_date")
    j = tt.join_asof(dd, left_on="date", right_on="d_date", by=["opp", "season"], strategy="backward",
                     allow_exact_matches=False)
    j = j.with_columns([pl.when(pl.col("n_after") >= min_g).then(
        ((pl.col(f"q_{c}") / pl.col("n_after")) / (pl.col(f"lp_{c}") / pl.col("lpn").clip(1, None)) * pl.col("n_after") + k)
        / (pl.col("n_after") + k)).otherwise(None).alias(f"f_{c}") for c in ZONES])
    # player profile: points by zone in his games before the date (this season + last)
    pp = z.group_by(["player_id", "date"]).agg([pl.col(c).sum() for c in ZONES]).sort("date")
    pp = pp.with_columns([pl.col(c).cum_sum().over("player_id").alias(f"c_{c}") for c in ZONES]).select(
        pl.col("player_id").cast(pl.Int64), pl.col("date").cast(pl.Date).alias("p_date"), *[f"c_{c}" for c in ZONES]).sort("p_date")
    j = j.sort("date").join_asof(pp, left_on="date", right_on="p_date", by="player_id", strategy="backward",
                                 allow_exact_matches=False)
    tot = sum(pl.col(f"c_{c}").fill_null(0) for c in ZONES)
    j = j.with_columns(z_sig=pl.when(tot >= 50).then(sum(pl.col(f"c_{c}").fill_null(0) / tot * (pl.col(f"f_{c}") - 1) for c in ZONES))
                       .otherwise(None), z_three=pl.col("f_three") - 1)
    return j.select("game_id", "player_id", "z_sig", "z_three")


def _cast_join_keys(t: pl.DataFrame, zf: pl.DataFrame) -> pl.DataFrame:
    return zf.with_columns(pl.col("game_id").cast(t.schema["game_id"]), pl.col("player_id").cast(t.schema["player_id"]))
