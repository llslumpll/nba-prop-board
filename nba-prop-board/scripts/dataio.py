"""
Data layer: downloads and caches the free sportsdataverse ESPN parquet
releases, cleans them into one tidy player-game table and one schedule
table, all keyed by ESPN game_id and canonical team codes (teams.py).

Season labels are the year a season ENDS: 2026-27 is 2027.

  history   player_box_2024..2026   pulled once, cached in data/cache/
  current   player_box_2027         404 until games are played; handled
  schedule  nba_schedule_2027       refreshed every heavy build

Nothing is ever fabricated: a failed download keeps the last cached copy
and is recorded in data/source_status.json, which the Home page reads.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import polars as pl
import requests

import teams

ROOT = Path(__file__).parent.parent
DATA_DIR = ROOT / "data"
CACHE = DATA_DIR / "cache"
STATUS_PATH = DATA_DIR / "source_status.json"

CURRENT_SEASON = 2027
HISTORY_SEASONS = (2024, 2025, 2026)
REGULAR, PLAYOFFS, PLAY_IN = 2, 3, 5

BASE = "https://github.com/sportsdataverse/sportsdataverse-data/releases/download"
BOX_URL = BASE + "/espn_nba_player_boxscores/player_box_{season}.parquet"
SCHED_URL = BASE + "/espn_nba_schedules/nba_schedule_{season}.parquet"

STAT_COLS = ("pts", "reb", "ast", "fg3m", "stl", "blk", "tov")


def _now_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


# ---------------------------------------------------------------------------
# Download / cache
# ---------------------------------------------------------------------------

def _load_status() -> dict:
    try:
        return json.loads(STATUS_PATH.read_text())
    except Exception:
        return {}


def _save_status(status: dict) -> None:
    STATUS_PATH.write_text(json.dumps(status, indent=2, sort_keys=True))


def _download(url: str, dest: Path) -> dict:
    """Fetch url to dest atomically. Never raises. Returns a status dict with
    the HTTP code and the server's Last-Modified time (when the upstream file
    was last rebuilt -- this is how the site knows if data is stale)."""
    rec = {"url": url, "checked_at": _now_str()}
    try:
        r = requests.get(url, timeout=60, allow_redirects=True)
        rec["http"] = r.status_code
        rec["last_modified"] = r.headers.get("Last-Modified")
        if r.status_code == 404:
            rec["state"] = "not_published"
            return rec
        r.raise_for_status()
        tmp = dest.with_suffix(".tmp")
        tmp.write_bytes(r.content)
        pl.read_parquet(tmp, n_rows=1)  # refuse to replace a good cache with a bad file
        tmp.replace(dest)
        rec["state"] = "ok"
        rec["bytes"] = len(r.content)
    except Exception as e:  # noqa: BLE001
        rec["state"] = "error"
        rec["error"] = f"{type(e).__name__}: {str(e)[:200]}"
    return rec


def refresh(full: bool = True) -> dict:
    """Heavy-build data pull. History is pulled only if missing."""
    CACHE.mkdir(parents=True, exist_ok=True)
    status = _load_status()
    jobs = [(f"schedule_{CURRENT_SEASON}", SCHED_URL.format(season=CURRENT_SEASON), True),
            (f"player_box_{CURRENT_SEASON}", BOX_URL.format(season=CURRENT_SEASON), True)]
    for s in HISTORY_SEASONS:
        jobs.append((f"player_box_{s}", BOX_URL.format(season=s), False))
    for name, url, always in jobs:
        dest = CACHE / (url.rsplit("/", 1)[-1])
        if not always and dest.exists():
            continue
        if not full and dest.exists():
            continue
        rec = _download(url, dest)
        status[name] = rec
        print(f"  {name}: {rec.get('state')} (HTTP {rec.get('http')}, upstream modified {rec.get('last_modified')})")
    _save_status(status)
    clear_caches()
    return status


def source_status() -> dict:
    return _load_status()


# ---------------------------------------------------------------------------
# Cleaned tables
# ---------------------------------------------------------------------------

_box_cache: pl.DataFrame | None = None
_sched_cache: pl.DataFrame | None = None


def clear_caches() -> None:
    global _box_cache, _sched_cache
    _box_cache = None
    _sched_cache = None


def _espn_code_expr(col: str) -> pl.Expr:
    return pl.col(col).replace_strict(
        {**{c: c for c in teams.CODES}, **teams.ALIASES["espn"]}, default=None)


def _clean_box(df: pl.DataFrame) -> pl.DataFrame:
    df = df.filter(pl.col("season_type").is_in([REGULAR, PLAYOFFS, PLAY_IN]))
    df = df.with_columns(
        team=_espn_code_expr("team_abbreviation"),
        opp=_espn_code_expr("opponent_team_abbreviation"),
    ).filter(pl.col("team").is_not_null() & pl.col("opp").is_not_null())  # drops All-Star teams
    mins = pl.col("minutes").fill_null(0.0)
    df = df.with_columns(
        played=(~pl.col("did_not_play").fill_null(False)) & (mins > 0),
    )
    pos = pl.col("athlete_position_abbreviation").fill_null("F").str.slice(-1, 1)
    out = df.select(
        pl.col("athlete_display_name").alias("player"),
        pl.col("athlete_id").cast(pl.Int64).alias("player_id"),
        pl.col("team"), pl.col("opp"),
        pl.col("game_id").cast(pl.Int64),
        pl.col("game_date").alias("date"),
        pl.col("season").cast(pl.Int32),
        pl.col("season_type").cast(pl.Int32),
        pl.when(pos.is_in(["G", "F", "C"])).then(pos).otherwise(pl.lit("F")).alias("pos"),
        pl.col("starter").fill_null(False),
        (pl.col("home_away") == "home").alias("home"),
        pl.col("played"),
        mins.alias("min"),
        pl.col("points").fill_null(0).alias("pts"),
        pl.col("rebounds").fill_null(0).alias("reb"),
        pl.col("assists").fill_null(0).alias("ast"),
        pl.col("three_point_field_goals_made").fill_null(0).alias("fg3m"),
        pl.col("three_point_field_goals_attempted").fill_null(0).alias("fg3a"),
        pl.col("field_goals_attempted").fill_null(0).alias("fga"),
        pl.col("free_throws_attempted").fill_null(0).alias("fta"),
        pl.col("offensive_rebounds").fill_null(0).alias("oreb"),
        pl.col("steals").fill_null(0).alias("stl"),
        pl.col("blocks").fill_null(0).alias("blk"),
        pl.col("turnovers").fill_null(0).alias("tov"),
        pl.col("team_score").cast(pl.Int32, strict=False),
        pl.col("opponent_team_score").cast(pl.Int32, strict=False).alias("opp_score"),
    )
    return out.with_columns(pl.col([c for c in ("pts", "reb", "ast", "fg3m", "fg3a", "fga", "fta", "oreb", "stl", "blk", "tov")]).cast(pl.Int32))


def load_box() -> pl.DataFrame:
    """Every cached player-game row (history + current season if published),
    including DNP rows (played == False) -- those matter for knowing who was
    out. Sorted by date."""
    global _box_cache
    if _box_cache is None:
        frames = []
        for s in (*HISTORY_SEASONS, CURRENT_SEASON):
            p = CACHE / f"player_box_{s}.parquet"
            if p.exists():
                try:
                    frames.append(_clean_box(pl.read_parquet(p)))
                except Exception as e:  # noqa: BLE001
                    print(f"WARNING: could not read {p.name}: {e}")
        _box_cache = (pl.concat(frames, how="vertical_relaxed").sort(["date", "game_id"])
                      if frames else pl.DataFrame())
    return _box_cache


def current_season_rows() -> pl.DataFrame:
    b = load_box()
    return b.filter(pl.col("season") == CURRENT_SEASON) if b.height else b


def has_current_season_data() -> bool:
    return current_season_rows().height > 0


def load_schedule() -> pl.DataFrame:
    """Current-season schedule with canonical codes and UTC tip times.
    `tip_utc` is null when ESPN hasn't set a valid time (time_valid False)."""
    global _sched_cache
    if _sched_cache is None:
        p = CACHE / f"nba_schedule_{CURRENT_SEASON}.parquet"
        if not p.exists():
            _sched_cache = pl.DataFrame()
            return _sched_cache
        s = pl.read_parquet(p)
        s = s.with_columns(
            home=_espn_code_expr("home_abbreviation"),
            away=_espn_code_expr("away_abbreviation"),
        )
        s = s.select(
            pl.col("game_id").cast(pl.Int64),
            pl.col("season_type").cast(pl.Int32),
            pl.col("game_date").alias("date"),
            pl.col("game_date_time").dt.convert_time_zone("UTC").alias("tip_utc"),
            pl.col("time_valid").fill_null(True),
            pl.col("home"), pl.col("away"),
            pl.col("home_abbreviation").alias("home_raw"),
            pl.col("away_abbreviation").alias("away_raw"),
            pl.col("status_type_completed").fill_null(False).alias("completed"),
            pl.col("status_type_name").alias("status"),
            pl.col("home_score").cast(pl.Int32, strict=False),
            pl.col("away_score").cast(pl.Int32, strict=False),
            pl.col("neutral_site").fill_null(False),
            pl.col("notes_headline"),
        ).with_columns(
            pl.when(pl.col("time_valid")).then(pl.col("tip_utc")).otherwise(None).alias("tip_utc")
        ).sort(["tip_utc", "game_id"], nulls_last=True)
        for raw in s.filter(pl.col("home").is_null())["home_raw"].unique().to_list():
            if raw and raw != "TBD":
                teams.note_unmatched("espn", raw)
        _sched_cache = s
    return _sched_cache


def season_status(now: datetime | None = None) -> dict:
    """Computed, never hardcoded: what phase the season is in, games played,
    days of data loaded. Used for every count on the Home page."""
    now = now or datetime.now(timezone.utc)
    sched = load_schedule()
    cur = current_season_rows()
    hist = load_box().filter(pl.col("season") != CURRENT_SEASON) if load_box().height else pl.DataFrame()
    played_games = cur.filter(pl.col("season_type") == REGULAR)["game_id"].n_unique() if cur.height else 0
    total_games = sched.filter((pl.col("season_type") == REGULAR) & pl.col("home").is_not_null()).height if sched.height else 0
    first_tip = sched["tip_utc"].drop_nulls().min() if sched.height else None
    if first_tip is not None and now < first_tip:
        phase = "preseason"
        days = (first_tip.date() - now.date()).days
        phase_text = f"Season opens in {days} day{'s' if days != 1 else ''}" if days > 0 else "Season opens today"
    elif played_games and total_games and played_games >= total_games:
        phase, phase_text = "complete", "Regular season complete"
    else:
        phase, phase_text = "in_season", f"{played_games} of {total_games} regular-season games in the data"
    return {
        "phase": phase,
        "phase_text": phase_text,
        "games_played": played_games,
        "games_total": total_games,
        "days_loaded": cur["date"].n_unique() if cur.height else 0,
        "last_data_date": str(cur["date"].max()) if cur.height else None,
        "history_games": hist["game_id"].n_unique() if hist.height else 0,
        "history_seasons": sorted(hist["season"].unique().to_list()) if hist.height else [],
        "first_tip": first_tip,
    }


def upcoming_games(now: datetime | None = None, hours: float = 36) -> pl.DataFrame:
    """Scheduled, not-yet-started games tipping within `hours` -- the slate the
    site projects. Games with no valid tip time are excluded (we can't enforce
    the never-freeze-after-tip rule without one)."""
    now = now or datetime.now(timezone.utc)
    s = load_schedule()
    if not s.height:
        return s
    from datetime import timedelta
    return s.filter(
        pl.col("tip_utc").is_not_null()
        & (pl.col("tip_utc") > now)
        & (pl.col("tip_utc") <= now + timedelta(hours=hours))
        & pl.col("home").is_not_null() & pl.col("away").is_not_null()
        & ~pl.col("completed")
    )


def next_slate(now: datetime | None = None) -> pl.DataFrame:
    """Every game on the next calendar day (US Eastern) that has games, so the
    site always has a slate even in the four days before opening night."""
    now = now or datetime.now(timezone.utc)
    s = load_schedule()
    if not s.height:
        return s
    fut = s.filter(pl.col("tip_utc").is_not_null() & (pl.col("tip_utc") > now)
                   & pl.col("home").is_not_null() & pl.col("away").is_not_null())
    if not fut.height:
        return fut
    first_date = fut["date"].min()
    return fut.filter(pl.col("date") == first_date)
