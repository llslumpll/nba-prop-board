"""
Starting lineups and official inactives from NBA.com's daily lineups file
(the data behind nba.com "Today's Starting Lineups"; no key):
  https://stats.nba.com/js/data/leaders/00_daily_lineups_YYYYMMDD.json

Expected shape: {"games": [{"gameId", "homeTeam": {"teamAbbreviation",
"players": [{"playerName" | "firstName"+"lastName", "lineupStatus"
("Confirmed" | "Expected"), "position" (set for the five starters),
"rosterStatus" ("Active" | "Inactive")}]}, "awayTeam": {...}}]}.
Parsed defensively: any field that's missing is skipped, and the first game's
raw keys are saved so a format change shows up in data/lineups.json.

How the build uses it (the "final check" before tip):
  * a player on the official inactive list of a team whose lineup is
    CONFIRMED is treated as out (his minutes go to teammates), unless he's
    listed as a starter. Capped at 10 per team as a guard against a bad file.
  * confirmed / expected starters get a STARTING tag on Players and Picks.
Names are matched to our ESPN roster within the team (accents and suffixes
ignored). Fail-soft: on any problem the build carries on without it.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import requests

import errs
import teams
from markets import normalize_name

URL = "https://stats.nba.com/js/data/leaders/00_daily_lineups_{d}.json"
PATH = Path(__file__).parent.parent / "data" / "lineups.json"
HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
           "Referer": "https://www.nba.com/", "Origin": "https://www.nba.com", "Accept": "application/json, text/plain, */*"}
MAX_INACTIVE_PER_TEAM = 10


def _name(p: dict) -> str | None:
    n = p.get("playerName") or p.get("name")
    if not n and (p.get("firstName") or p.get("lastName")):
        n = f"{p.get('firstName') or ''} {p.get('lastName') or ''}".strip()
    return n or None


def parse(raw: dict, roster: list[dict]) -> dict:
    by_team = {}
    for r in roster:
        if r.get("team") and r.get("player"):
            by_team.setdefault(r["team"], {})[normalize_name(r["player"])] = r["player"]
    teams_out, inactive, unmatched = {}, [], []
    games = raw.get("games") or []
    for g in games:
        for side in ("homeTeam", "awayTeam"):
            t = g.get(side) or {}
            code = teams.canon(t.get("teamAbbreviation") or t.get("teamTricode"), "kalshi")
            if not code:
                continue
            names = by_team.get(code, {})
            starters, inact, statuses = [], [], set()
            for p in t.get("players") or []:
                nm = _name(p)
                if not nm:
                    continue
                ours = names.get(normalize_name(nm))
                if not ours:
                    unmatched.append(f"{code} {nm}")
                    ours = nm
                ls = str(p.get("lineupStatus") or "").strip()
                if ls:
                    statuses.add(ls.title())
                if str(p.get("position") or "").strip() and str(p.get("rosterStatus") or "Active").lower() != "inactive":
                    starters.append(ours)
                if str(p.get("rosterStatus") or "").lower() == "inactive":
                    inact.append(ours)
            status = "Confirmed" if "Confirmed" in statuses else ("Expected" if statuses or starters else None)
            teams_out[code] = {"status": status, "starters": starters[:5], "inactive": inact, "game_id": g.get("gameId")}
            if status == "Confirmed" and len(inact) <= MAX_INACTIVE_PER_TEAM:
                inactive += [{"player": n, "team": code} for n in inact if n not in starters]
    return {"teams": teams_out, "inactive": inactive, "unmatched": unmatched[:40], "games": len(games)}


def fetch(date_et: str, roster: list[dict]) -> dict:
    """date_et = 'YYYY-MM-DD' (the slate date)."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    try:
        r = requests.get(URL.format(d=date_et.replace("-", "")), timeout=8, headers=HEADERS)
        r.raise_for_status()
        raw = r.json()
        out = {"fetched_at": now, "date": date_et, "error": None, **parse(raw, roster)}
        g0 = (raw.get("games") or [{}])[0]
        t0 = g0.get("homeTeam") or {}
        out["format"] = {"top": sorted(raw)[:10], "game": sorted(g0)[:15], "team": sorted(t0)[:15],
                         "player": sorted((t0.get("players") or [{}])[0])[:15]}
        PATH.write_text(json.dumps(out, indent=1))
        return out
    except Exception as e:  # noqa: BLE001
        prev = load()
        if prev.get("date") != date_et:
            prev = {"date": date_et, "teams": {}, "inactive": [], "games": 0}
        prev["error"] = f"{errs.short(e)} (showing copy from {prev.get('fetched_at') or 'never'})"
        return prev


def load() -> dict:
    try:
        return json.loads(PATH.read_text())
    except Exception:  # noqa: BLE001
        return {"fetched_at": None, "error": "no lineups fetched yet", "teams": {}, "inactive": []}


def merge(inj: dict, lu: dict) -> tuple[dict, int]:
    """Add official inactives (confirmed lineups only) to the injury report as OUT, and the
    starters map for the STARTING tag. Returns (new report, players newly ruled out)."""
    inj = dict(inj)
    players = list(inj.get("players") or [])
    known = {(p.get("player"), p.get("team")): i for i, p in enumerate(players)}
    added = 0
    for x in lu.get("inactive") or []:
        k = (x["player"], x["team"])
        if k in known:
            p = dict(players[known[k]])
            if p.get("class") != "out":
                p.update(cls_was=p.get("class"), status="Inactive (official)", **{"class": "out"})
                p["comment"] = ((p.get("comment") or "") + " | NBA official inactive list").strip(" |")
                players[known[k]] = p
                added += 1
        else:
            players.append({"player": x["player"], "team": x["team"], "status": "Inactive (official)", "class": "out",
                            "date": None, "comment": "NBA official inactive list"})
            added += 1
    inj["players"] = players
    st = {}
    for code, t in (lu.get("teams") or {}).items():
        for n in t.get("starters") or []:
            st[n] = t.get("status") or "Expected"
    inj["starters"] = st
    return inj, added
