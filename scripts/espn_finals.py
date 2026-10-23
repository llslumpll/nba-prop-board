"""
Same-night results from ESPN, so picks are graded minutes after each game
ends instead of the next morning.

The daily sportsdataverse box-score file only updates once a day (~2 AM CT).
Every build (light or heavy) now also asks ESPN's public scoreboard which
games are live / final for the dates we care about (today and yesterday in
US Eastern time, plus any date that still has pending picks). For each FINAL
game that isn't in the daily file yet, it downloads that game's box score
once and keeps it in data/espn_box.json. Those rows are added to the box
table in memory so grading, the Games page and the model all see the result
straight away. When the daily file catches up, its rows win and the ESPN copy
is dropped.

ESPN game ids are the same ids the schedule uses, and ESPN athlete ids are
our player_ids, so nothing has to be name-matched.

Never raises: if ESPN is unreachable the site simply waits for the daily file
like before, and the reason is written to data/espn_box.json -> "error".
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import polars as pl
import requests

import teams

ET = ZoneInfo("America/New_York")
API = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/"
PATH = Path(__file__).parent.parent / "data" / "espn_box.json"
KEEP_DAYS = 10
HDR = {"User-Agent": "Mozilla/5.0"}


def _get(url: str) -> dict:
    r = requests.get(url, timeout=20, headers=HDR)
    r.raise_for_status()
    return r.json()


def _load() -> dict:
    try:
        return json.loads(PATH.read_text())
    except Exception:  # noqa: BLE001
        return {}


def parse_scoreboard(j: dict) -> dict:
    """{game_id: {state, completed, home, away, home_score, away_score, detail}}"""
    out = {}
    for e in j.get("events") or []:
        c = (e.get("competitions") or [{}])[0]
        st = c.get("status") or e.get("status") or {}
        ty = st.get("type") or {}
        h = a = {}
        for t in c.get("competitors") or []:
            if t.get("homeAway") == "home":
                h = t
            else:
                a = t
        try:
            gid = int(e.get("id"))
        except (TypeError, ValueError):
            continue
        out[gid] = {
            "state": ty.get("state") or "pre",
            "completed": bool(ty.get("completed")) or ty.get("state") == "post",
            "home": teams.canon(((h.get("team") or {}).get("abbreviation")), "espn") if h.get("team") else None,
            "away": teams.canon(((a.get("team") or {}).get("abbreviation")), "espn") if a.get("team") else None,
            "home_score": _int(h.get("score")), "away_score": _int(a.get("score")),
            "detail": ty.get("shortDetail") or ty.get("detail") or "",
        }
    return out


def _int(v):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def _num(s, i):
    if i is None or i >= len(s):
        return 0
    v = str(s[i]).split("-")[0].replace("+", "")
    try:
        return int(float(v))
    except ValueError:
        return 0


def _att(s, i):
    if i is None or i >= len(s) or "-" not in str(s[i]):
        return 0
    try:
        return int(str(s[i]).split("-")[1])
    except ValueError:
        return 0


def parse_summary(sum_: dict, gid: int, game_date: str, season: int, season_type: int,
                  sb: dict | None = None) -> list[dict]:
    """Box rows in the same shape as dataio.load_box()."""
    sides, scores = {}, {}
    comp = ((sum_.get("header") or {}).get("competitions") or [{}])[0]
    for c in comp.get("competitors") or []:
        tid = str((c.get("team") or {}).get("id") or "")
        sides[tid] = c.get("homeAway")
        scores[c.get("homeAway")] = _int(c.get("score"))
    if sb:
        scores.setdefault("home", sb.get("home_score"))
        scores.setdefault("away", sb.get("away_score"))
        if scores.get("home") is None:
            scores["home"] = sb.get("home_score")
        if scores.get("away") is None:
            scores["away"] = sb.get("away_score")
    blocks = (sum_.get("boxscore") or {}).get("players") or []
    codes = [teams.canon(((b.get("team") or {}).get("abbreviation")), "espn") for b in blocks]
    rows = []
    for bi, b in enumerate(blocks):
        team = codes[bi]
        opp = codes[1 - bi] if len(codes) == 2 else None
        tid = str((b.get("team") or {}).get("id") or "")
        side = sides.get(tid) or (("home" if team == sb.get("home") else "away") if sb else ("home" if bi == 1 else "away"))
        if not team or not opp:
            continue
        st = (b.get("statistics") or [{}])[0]
        keys, names = st.get("keys") or [], st.get("names") or st.get("labels") or []

        def idx(k, n):
            if k in keys:
                return keys.index(k)
            return names.index(n) if n in names else None
        i = {"min": idx("minutes", "MIN"), "pts": idx("points", "PTS"), "reb": idx("rebounds", "REB"),
             "ast": idx("assists", "AST"), "fg": idx("fieldGoalsMade-fieldGoalsAttempted", "FG"),
             "fg3": idx("threePointFieldGoalsMade-threePointFieldGoalsAttempted", "3PT"),
             "ft": idx("freeThrowsMade-freeThrowsAttempted", "FT"), "oreb": idx("offensiveRebounds", "OREB"),
             "stl": idx("steals", "STL"), "blk": idx("blocks", "BLK"), "tov": idx("turnovers", "TO")}
        for a in st.get("athletes") or []:
            ath = a.get("athlete") or {}
            try:
                pid = int(ath.get("id"))
            except (TypeError, ValueError):
                continue
            s = a.get("stats") or []
            mins = float(_num(s, i["min"])) if s else 0.0
            pos = ((ath.get("position") or {}).get("abbreviation") or "F")[-1:]
            rows.append({
                "player": ath.get("displayName") or "", "player_id": pid, "team": team, "opp": opp,
                "game_id": gid, "date": game_date, "season": season, "season_type": season_type,
                "pos": pos if pos in ("G", "F", "C") else "F", "starter": bool(a.get("starter")),
                "home": side == "home", "played": (not a.get("didNotPlay")) and bool(s) and mins > 0, "min": mins,
                "pts": _num(s, i["pts"]), "reb": _num(s, i["reb"]), "ast": _num(s, i["ast"]),
                "fg3m": _num(s, i["fg3"]), "fg3a": _att(s, i["fg3"]), "fga": _att(s, i["fg"]), "fta": _att(s, i["ft"]),
                "oreb": _num(s, i["oreb"]), "stl": _num(s, i["stl"]), "blk": _num(s, i["blk"]), "tov": _num(s, i["tov"]),
                "team_score": scores.get(side), "opp_score": scores.get("away" if side == "home" else "home"),
            })
    return rows


def refresh(sched: pl.DataFrame, box: pl.DataFrame, pending_dates: set[str], now: datetime | None = None) -> dict:
    """Ask ESPN about recent dates; download box scores for new finals.
    Returns the stored dict {"at", "error", "status": {gid: {...}}, "games": {gid: {"date", "rows"}}}."""
    now = now or datetime.now(timezone.utc)
    store = _load()
    store.setdefault("games", {})
    today = now.astimezone(ET).date()
    dates = {today, today - timedelta(days=1)}
    for d in pending_dates:
        try:
            dd = date.fromisoformat(d)
        except ValueError:
            continue
        if today - timedelta(days=4) <= dd <= today:
            dates.add(dd)
    in_box = set(box["game_id"].unique().to_list()) if box.height else set()
    meta = {}
    if sched.height:
        for r in sched.select("game_id", "date", "season_type").iter_rows(named=True):
            meta[r["game_id"]] = (str(r["date"]), r["season_type"])
    status, errors, fetched = {}, [], 0
    for d in sorted(dates):
        try:
            status.update(parse_scoreboard(_get(API + "scoreboard?dates=" + d.strftime("%Y%m%d"))))
        except Exception as e:  # noqa: BLE001
            errors.append(f"scoreboard {d}: {type(e).__name__}: {str(e)[:120]}")
    for gid, s in status.items():
        if not s["completed"] or gid in in_box or str(gid) in store["games"]:
            continue
        gdate, stype = meta.get(gid, (None, 2))
        if not gdate:
            continue
        try:
            rows = parse_summary(_get(API + f"summary?event={gid}"), gid, gdate, _season(gdate), stype or 2, s)
        except Exception as e:  # noqa: BLE001
            errors.append(f"summary {gid}: {type(e).__name__}: {str(e)[:120]}")
            continue
        if sum(1 for r in rows if r["played"]) >= 10:  # a real final box has 10+ players who played
            store["games"][str(gid)] = {"date": gdate, "rows": rows, "final": s}
            fetched += 1
    # forget games the daily file now has, and anything old
    cutoff = (today - timedelta(days=KEEP_DAYS)).isoformat()
    store["games"] = {k: v for k, v in store["games"].items() if int(k) not in in_box and v.get("date", "") >= cutoff}
    store["status"] = {str(k): v for k, v in status.items()}
    store["at"] = now.strftime("%Y-%m-%d %H:%M UTC")
    store["error"] = "; ".join(errors)[:600] or None
    store["new_finals"] = fetched
    PATH.write_text(json.dumps(store, indent=1, sort_keys=True, default=str))
    return store


def _season(gdate: str) -> int:
    d = date.fromisoformat(gdate)
    return d.year + 1 if d.month >= 8 else d.year


def merge_box(box: pl.DataFrame, store: dict) -> pl.DataFrame:
    """box + ESPN rows for finals the daily file doesn't have yet."""
    rows = [r for g in (store.get("games") or {}).values() for r in g.get("rows", [])]
    if not rows or not box.height:
        return box
    extra = pl.DataFrame(rows).with_columns(pl.col("date").str.to_date())
    extra = extra.select([pl.col(c).cast(box.schema[c], strict=False) if c in extra.columns else
                          pl.lit(None, dtype=box.schema[c]).alias(c) for c in box.columns])
    extra = extra.filter(~pl.col("game_id").is_in(box["game_id"].unique().implode()))
    return pl.concat([box, extra], how="vertical_relaxed").sort(["date", "game_id"]) if extra.height else box


def merge_schedule(sched: pl.DataFrame, store: dict) -> pl.DataFrame:
    """Mark ESPN finals completed (with final scores) and record live state."""
    st = store.get("status") or {}
    if not st or not sched.height:
        return sched
    done = {int(k): v for k, v in st.items() if v.get("completed") and v.get("home_score") is not None}
    if not done:
        return sched
    ids = list(done)
    upd = pl.DataFrame({"game_id": ids, "_hs": [done[i]["home_score"] for i in ids],
                        "_as": [done[i]["away_score"] for i in ids]},
                       schema={"game_id": pl.Int64, "_hs": pl.Int32, "_as": pl.Int32})
    s = sched.join(upd, on="game_id", how="left")
    return s.with_columns(
        completed=pl.col("completed") | pl.col("_hs").is_not_null(),
        home_score=pl.coalesce(pl.col("_hs"), pl.col("home_score")),
        away_score=pl.coalesce(pl.col("_as"), pl.col("away_score")),
        status=pl.when(pl.col("_hs").is_not_null()).then(pl.lit("STATUS_FINAL")).otherwise(pl.col("status")),
    ).drop("_hs", "_as")
