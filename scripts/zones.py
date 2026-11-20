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


def path_x(season: int) -> Path:
    return dataio.CACHE / f"zones_x_{season}.parquet"


TEAM_FIX = {"GS": "GSW", "NO": "NOP", "NY": "NYK", "SA": "SAS", "UTAH": "UTA", "WSH": "WAS"}


def summarize_x(pbp: pl.DataFrame) -> pl.DataFrame:
    """Update 90 (Nov 20 2026): zones for the other stats, per player-game vs the defense he faced.
      a_rim / a_mid / a_three   assists, by the shot the pass set up
      r_rim / r_mid / r_three   rebounds, by where the missed shot came from (free-throw misses count as rim:
                                short boards); oreb / dreb = offensive / defensive rebounds
      fg3a                      threes attempted"""
    p = pbp.sort(["game_id", "sequence_number"]).with_columns(
        zone=pl.when(pl.col("type_text").str.contains("Free Throw")).then(pl.lit("rim"))
        .when(pl.col("points_attempted") == 3).then(pl.lit("three"))
        .when(pl.col("type_text").str.contains(RIM_RE)).then(pl.lit("rim")).otherwise(pl.lit("mid")),
        opp=pl.when(pl.col("team_id") == pl.col("home_team_id")).then(pl.col("away_team_abbrev"))
        .otherwise(pl.col("home_team_abbrev")))
    p = p.with_columns(prev_zone=pl.col("zone").shift(1).over("game_id"))
    keys = ["game_id", "game_date", "season", "season_type", "pid", "opp"]
    ast = p.filter(pl.col("scoring_play") & pl.col("shooting_play") & pl.col("athlete_id_2").is_not_null()
                   & ~pl.col("type_text").str.contains("Free Throw")).with_columns(pid=pl.col("athlete_id_2"))
    ast = ast.group_by(keys).agg([(pl.col("zone") == z).sum().alias(f"a_{z}") for z in ZONES])
    rb = p.filter(pl.col("type_text").str.contains("Rebound") & pl.col("athlete_id_1").is_not_null()).with_columns(
        pid=pl.col("athlete_id_1"), pz=pl.col("prev_zone").fill_null("rim"))
    rb = rb.group_by(keys).agg([(pl.col("pz") == z).sum().alias(f"r_{z}") for z in ZONES]
                               + [(pl.col("type_text") == "Offensive Rebound").sum().alias("oreb"),
                                  (pl.col("type_text") == "Defensive Rebound").sum().alias("dreb")])
    t3 = p.filter(pl.col("shooting_play") & (pl.col("points_attempted") == 3) & pl.col("athlete_id_1").is_not_null()
                  & ~pl.col("type_text").str.contains("Free Throw")).with_columns(pid=pl.col("athlete_id_1"))
    t3 = t3.group_by(keys).agg(pl.len().alias("fg3a"))
    cols = [f"a_{z}" for z in ZONES] + [f"r_{z}" for z in ZONES] + ["oreb", "dreb", "fg3a"]
    out = ast.join(rb, on=keys, how="full", coalesce=True).join(t3, on=keys, how="full", coalesce=True)
    return out.rename({"game_date": "date", "pid": "player_id", "opp": "def_team"}).with_columns(
        pl.col("game_id").cast(pl.Int64), pl.col("player_id").cast(pl.Int64), pl.col("def_team").replace(TEAM_FIX),
        *[pl.col(c).fill_null(0).cast(pl.Int32) for c in cols]).sort(["date", "game_id"])


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
        if s != cur and path(s).exists() and path_x(s).exists():
            out[s] = "kept"
            continue
        try:
            lm = requests.head(url, timeout=30, allow_redirects=True).headers.get("Last-Modified")
            import playerlab
            if (path(s).exists() and path_x(s).exists() and lm and st.get(str(s)) == lm
                    and (s != cur or (playerlab.shots_path(s).exists() and playerlab.reb_path(s).exists()))):
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
                pbp = pl.read_parquet(f)
                z = summarize(pbp)
                try:
                    zx = summarize_x(pbp)
                    if zx.height:
                        zx.write_parquet(path_x(s))
                except Exception as e:  # noqa: BLE001
                    out[f"x_{s}"] = f"error: {str(e)[:100]}"
                if s == cur:  # Player Lab (update 81): every shot with zone + type, kept small
                    try:
                        import playerlab
                        out[f"shots_{s}"] = playerlab.save_shots(pbp, s)
                    except Exception as e:  # noqa: BLE001
                        out[f"shots_{s}"] = f"error: {str(e)[:100]}"
                del pbp
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


_page = None


def page_info() -> dict:
    """For player pages (shown from day one, unlike the model's use): each defense's points allowed per game by zone this
    season with its rank (1 = most), and each player's share of his points by zone (this season + last)."""
    global _page
    if _page is not None:
        return _page
    z = load()
    out = {"teams": {}, "players": {}}
    if not z.height:
        _page = out
        return out
    z = z.filter(pl.col("season_type") == 2) if "season_type" in z.columns else z
    cur = z.filter(pl.col("season") == dataio.CURRENT_SEASON)
    if cur.height:
        d = cur.group_by(["game_id", "def_team"]).agg([pl.col(c).sum() for c in ZONES]).group_by("def_team").agg(
            [pl.col(c).mean().alias(c) for c in ZONES] + [pl.len().alias("g")])
        lg = {c: d[c].mean() for c in ZONES}
        for c in ZONES:
            d = d.with_columns(pl.col(c).rank("ordinal", descending=True).alias(f"r_{c}"))
        for r in d.iter_rows(named=True):
            out["teams"][r["def_team"]] = {c: {"pg": round(r[c], 1), "rank": int(r[f"r_{c}"]), "vs": round(100 * (r[c] / lg[c] - 1))}
                                           for c in ZONES} | {"g": r["g"], "n": d.height}
    p = z.group_by("player_id").agg([pl.col(c).sum() for c in ZONES])
    for r in p.iter_rows(named=True):
        tot = sum(r[c] for c in ZONES)
        if tot >= 50:
            out["players"][r["player_id"]] = {c: round(100 * r[c] / tot) for c in ZONES}
    _page = out
    return out


ZONE_NAME = {"rim": "at the rim", "mid": "from midrange", "three": "from three"}


def matchup_line(player_id, opp: str, stat: str) -> dict | None:
    """The shot-zone line for a player page: his main scoring zone vs what the opponent allows there."""
    info = page_info()
    mix, tm = info["players"].get(player_id), info["teams"].get(opp)
    if not mix or not tm or stat not in ("pts", "fg3m", "pra", "pr", "pa"):
        return None
    zone = "three" if stat == "fg3m" else max(ZONES, key=lambda c: mix[c])
    t = tm[zone]
    return {"zone": ZONE_NAME[zone], "share": mix[zone], "mix": mix, "opp": opp, "pg": t["pg"], "rank": t["rank"],
            "vs": t["vs"], "g": tm["g"], "n": tm["n"], "live": tm["g"] >= 15}


# ---- update 90 (Nov 20 2026): a zone line for every prop, not just points --------------------------------
X_COLS = [f"a_{z}" for z in ZONES] + [f"r_{z}" for z in ZONES] + ["oreb", "dreb", "fg3a"]
_page_x = None


def _load_x() -> pl.DataFrame:
    fr = [pl.read_parquet(path_x(s)) for s in (dataio.CURRENT_SEASON - 1, dataio.CURRENT_SEASON) if path_x(s).exists()]
    return pl.concat(fr, how="vertical_relaxed") if fr else pl.DataFrame()


def page_info_x() -> dict:
    """Each defense this season: assists allowed by the shot they set up, rebounds allowed by where the miss came from,
    offensive / defensive boards allowed, threes attempted against it, per game with rank (1 = most). Each player
    (this season + last): his assists and rebounds split the same way."""
    global _page_x
    if _page_x is not None:
        return _page_x
    out = {"teams": {}, "players": {}}
    z = _load_x()
    if not z.height:
        _page_x = out
        return out
    z = z.filter(pl.col("season_type") == 2) if "season_type" in z.columns else z
    cur = z.filter(pl.col("season") == dataio.CURRENT_SEASON)
    if cur.height:
        d = cur.group_by(["game_id", "def_team"]).agg([pl.col(c).sum() for c in X_COLS]).group_by("def_team").agg(
            [pl.col(c).mean() for c in X_COLS] + [pl.len().alias("g")])
        lg = {c: d[c].mean() for c in X_COLS}
        d = d.with_columns([pl.col(c).rank("ordinal", descending=True).alias(f"rk_{c}") for c in X_COLS])
        for r in d.iter_rows(named=True):
            out["teams"][r["def_team"]] = {c: {"pg": round(r[c], 1), "rank": int(r[f"rk_{c}"]),
                                               "vs": round(100 * (r[c] / lg[c] - 1)) if lg[c] else 0} for c in X_COLS} \
                | {"g": r["g"], "n": d.height}
    p = z.group_by("player_id").agg([pl.col(c).sum() for c in X_COLS])
    tot = z.select([pl.col(c).sum() for c in X_COLS]).row(0, named=True)
    for k in ("a", "r"):  # league mix: a player's zone is the one he leans on MORE than the league, not just the biggest
        t = sum(tot[f"{k}_{c}"] for c in ZONES) or 1
        out[f"lg_{k}"] = {c: round(100 * tot[f"{k}_{c}"] / t) for c in ZONES}
    for r in p.iter_rows(named=True):
        rec = {}
        ta = sum(r[f"a_{c}"] for c in ZONES)
        if ta >= 25:
            rec["ast"] = {c: round(100 * r[f"a_{c}"] / ta) for c in ZONES}
        tr = sum(r[f"r_{c}"] for c in ZONES)
        if tr >= 25:
            rec["reb"] = {c: round(100 * r[f"r_{c}"] / tr) for c in ZONES}
            rec["oreb"] = round(100 * r["oreb"] / max(1, r["oreb"] + r["dreb"]))
        if rec:
            out["players"][r["player_id"]] = rec
    _page_x = out
    return out


def _row(t: dict, **kw) -> dict:
    return {"pg": t["pg"], "rank": t["rank"], "vs": t["vs"], **kw}


def stat_zone(player_id, opp: str, stat: str) -> dict | None:
    """One zone line for a single stat (pts, reb, ast, fg3m)."""
    if stat in ("pts", "fg3m"):
        info = page_info()
        mix, tm = info["players"].get(player_id), info["teams"].get(opp)
        if not tm:
            return None
        if stat == "fg3m":
            tx = page_info_x()["teams"].get(opp) or {}
            t = tm["three"]
            out = {"stat": "fg3m", "title": "Threes", "opp": opp, "g": tm["g"], "n": tm["n"], "live": tm["g"] >= 15,
                   "main": _row({**t, "pg": round(t["pg"] / 3, 1)}, what="threes made a game", short="threes made"),
                   "mix": None, "share": mix["three"] if mix else None, "share_txt": "of his points come from three"}
            if tx.get("fg3a"):
                out["extra"] = _row(tx["fg3a"], what="three-point attempts a game", short="3PA")
            return out
        if not mix:
            return None
        zone = max(ZONES, key=lambda c: mix[c])
        return {"stat": "pts", "title": "Shot zones", "opp": opp, "g": tm["g"], "n": tm["n"], "live": tm["g"] >= 15,
                "mix": mix, "share": mix[zone], "share_txt": f"of his points come {ZONE_NAME[zone]}",
                "main": _row(tm[zone], what=f"points a game {ZONE_NAME[zone]}",
                             short={"rim": "pts at rim", "mid": "pts midrange", "three": "pts from 3"}[zone])}
    if stat not in ("reb", "ast"):
        return None
    info = page_info_x()
    pr, tm = (info["players"].get(player_id) or {}).get(stat), info["teams"].get(opp)
    if not tm:
        return None
    base = {"stat": stat, "opp": opp, "g": tm["g"], "n": tm["n"], "live": tm["g"] >= 15, "mix": pr}
    lgm = info.get("lg_a" if stat == "ast" else "lg_r") or {c: 33 for c in ZONES}
    if stat == "ast":
        if not pr:
            return None
        zone = max(ZONES, key=lambda c: pr[c] / max(1, lgm[c]))
        base["lg"] = lgm[zone]
        what = {"rim": "assists a game on shots at the rim", "mid": "assists a game on midrange shots",
                "three": "assists a game on threes"}[zone]
        return {**base, "title": "Assist zones", "share": pr[zone],
                "share_txt": {"rim": "of his assists set up shots at the rim", "mid": "of his assists set up midrange shots",
                              "three": "of his assists set up threes"}[zone],
                "main": _row(tm[f"a_{zone}"], what=what, short={"rim": "ast to rim", "mid": "ast midrange", "three": "ast to 3s"}[zone])}
    # rebounds: where the misses come from, plus offensive vs defensive boards
    oreb = (info["players"].get(player_id) or {}).get("oreb")
    if not pr:
        return None
    zone = max(ZONES, key=lambda c: pr[c] / max(1, lgm[c]))
    base["lg"] = lgm[zone]
    what = {"rim": "rebounds a game off misses at the rim (short boards)", "mid": "rebounds a game off midrange misses",
            "three": "rebounds a game off missed threes (long boards)"}[zone]
    side = "oreb" if (oreb or 0) >= 30 else "dreb"
    return {**base, "title": "Rebound zones", "share": pr[zone],
            "share_txt": {"rim": "of his rebounds come off misses at the rim", "mid": "of his rebounds come off midrange misses",
                          "three": "of his rebounds come off missed threes"}[zone],
            "main": _row(tm[f"r_{zone}"], what=what, short={"rim": "short boards", "mid": "mid boards", "three": "long boards"}[zone]),
            "oreb": oreb,
            "extra": _row(tm[side], what="offensive rebounds a game" if side == "oreb" else "defensive rebounds a game",
                          short="off. boards" if side == "oreb" else "def. boards")}


COMBO_PARTS = {"pra": ("pts", "reb", "ast"), "pr": ("pts", "reb"), "pa": ("pts", "ast"), "ra": ("reb", "ast")}


def zone_lines(player_id, opp: str, stat: str) -> list[dict]:
    """Every zone line that applies to a prop: one for a single stat, one per part for a combo."""
    out = []
    for s in COMBO_PARTS.get(stat, (stat,)):
        try:
            z = stat_zone(player_id, opp, s)
        except Exception:  # noqa: BLE001
            z = None
        if z:
            out.append(z)
    return out


def _sig(z: pl.DataFrame, cols: list[str], t: pl.DataFrame, name: str, min_g: int, k: float, min_tot: float) -> pl.DataFrame:
    """Walk-forward zone signal (same method as factors() for points): for each target row, the opponent's
    allowed-by-zone vs league (this season, games before the date, shrunk with k games, null below min_g games),
    weighted by the player's own mix over the same columns (this season + last, games before the date).
    Returns game_id, player_id, <name> = sum(share_z x (f_z - 1))."""
    d = z.group_by(["game_id", "date", "season", "def_team"]).agg([pl.col(c).sum() for c in cols]).sort("date")
    lg = d.group_by(["season", "date"]).agg([pl.col(c).sum().alias(f"s_{c}") for c in cols] + [pl.len().alias("n")]).sort("date")
    lg = lg.with_columns([(pl.col(f"s_{c}").cum_sum().over("season") - pl.col(f"s_{c}")).alias(f"lp_{c}") for c in cols]
                         + [(pl.col("n").cum_sum().over("season") - pl.col("n")).alias("lpn")])
    d = d.with_columns([(pl.col(c).cum_sum().over(["season", "def_team"]) - pl.col(c)).alias(f"p_{c}") for c in cols]
                       + [(pl.int_range(0, pl.len()).over(["season", "def_team"])).alias("pgp")])
    d = d.join(lg.select(["season", "date", "lpn"] + [f"lp_{c}" for c in cols]), on=["season", "date"])
    dd = d.select(pl.col("def_team").alias("opp"), pl.col("date").alias("d_date"), "season", "pgp",
                  *[pl.col(c).alias(f"g_{c}") for c in cols], *[f"p_{c}" for c in cols], "lpn", *[f"lp_{c}" for c in cols])
    dd = dd.with_columns([(pl.col(f"p_{c}") + pl.col(f"g_{c}")).alias(f"q_{c}") for c in cols]
                         + [(pl.col("pgp") + 1).alias("n_after")])
    tt = t.select(pl.col("game_id").cast(pl.Int64), pl.col("player_id").cast(pl.Int64), pl.col("opp").cast(pl.Utf8),
                  pl.col("date").cast(pl.Date), pl.col("season").cast(pl.Int64)).sort("date")
    dd = dd.with_columns(pl.col("d_date").cast(pl.Date), pl.col("season").cast(pl.Int64), pl.col("opp").cast(pl.Utf8)).sort("d_date")
    j = tt.join_asof(dd, left_on="date", right_on="d_date", by=["opp", "season"], strategy="backward", allow_exact_matches=False)
    j = j.with_columns([pl.when(pl.col("n_after") >= min_g).then(
        ((pl.col(f"q_{c}") / pl.col("n_after")) / (pl.col(f"lp_{c}") / pl.col("lpn").clip(1, None)) * pl.col("n_after") + k)
        / (pl.col("n_after") + k)).otherwise(None).alias(f"f_{c}") for c in cols])
    pp = z.group_by(["player_id", "date"]).agg([pl.col(c).sum() for c in cols]).sort("date")
    pp = pp.with_columns([pl.col(c).cum_sum().over("player_id").alias(f"c_{c}") for c in cols]).select(
        pl.col("player_id").cast(pl.Int64), pl.col("date").cast(pl.Date).alias("p_date"), *[f"c_{c}" for c in cols]).sort("p_date")
    j = j.sort("date").join_asof(pp, left_on="date", right_on="p_date", by="player_id", strategy="backward",
                                 allow_exact_matches=False)
    tot = sum(pl.col(f"c_{c}").fill_null(0) for c in cols)
    j = j.with_columns(pl.when(tot >= min_tot).then(
        sum(pl.col(f"c_{c}").fill_null(0) / tot * (pl.col(f"f_{c}") - 1) for c in cols)).otherwise(None).alias(name))
    return j.select("game_id", "player_id", name)


def factors_x(t: pl.DataFrame, min_g: int = 15, k: float = 10.0) -> pl.DataFrame:
    """Update 91 test: z_reb (rebounds by where the miss came from) and z_ast (assists by the shot set up)."""
    z = _load_x()
    sch = {"game_id": pl.Int64, "player_id": pl.Int64, "z_reb": pl.Float64, "z_ast": pl.Float64}
    if not z.height or not t.height:
        return pl.DataFrame(schema=sch)
    z = z.filter(pl.col("season_type") == 2) if "season_type" in z.columns else z
    r = _sig(z, [f"r_{c}" for c in ZONES], t, "z_reb", min_g, k, 25)
    a = _sig(z, [f"a_{c}" for c in ZONES], t, "z_ast", min_g, k, 25)
    return r.join(a, on=["game_id", "player_id"], how="full", coalesce=True)
