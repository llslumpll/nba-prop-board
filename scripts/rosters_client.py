"""
Current NBA rosters from ESPN's public site API (no key), pulled on the heavy
build so offseason trades and signings are right before box scores exist:
  https://site.api.espn.com/apis/site/v2/sports/basketball/nba/teams
  https://site.api.espn.com/apis/site/v2/sports/basketball/nba/teams/{id}/roster

Fail-soft: if the pull fails, the last good data/rosters.json is used; if
there has never been one, each player's team is his most recent box-score
team, and the page says so ("team from last box score").
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

import errs

import teams

BASE = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba"
PATH = Path(__file__).parent.parent / "data" / "rosters.json"
HEADERS = {"User-Agent": "Mozilla/5.0"}


def fetch() -> dict:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    try:
        r = requests.get(f"{BASE}/teams", timeout=20, headers=HEADERS)
        r.raise_for_status()
        tlist = []
        for sp in r.json().get("sports", []):
            for lg in sp.get("leagues", []):
                for t in lg.get("teams", []):
                    tm = t.get("team") or {}
                    tlist.append((tm.get("id"), tm.get("abbreviation")))
        players, failed = [], []
        for tid, abbr in tlist:
            code = teams.canon(abbr, "espn")
            if not tid or not code:
                continue
            try:
                rr = requests.get(f"{BASE}/teams/{tid}/roster", timeout=20, headers=HEADERS)
                rr.raise_for_status()
                for a in rr.json().get("athletes", []):
                    items = a.get("items") if isinstance(a, dict) and "items" in a else [a]
                    for it in items:
                        players.append({"player": it.get("displayName"), "player_id": int(it.get("id") or 0),
                                        "team": code, "pos": ((it.get("position") or {}).get("abbreviation"))})
            except Exception:  # noqa: BLE001
                failed.append(code)
            time.sleep(0.15)
        if len(players) < 300:
            raise ValueError(f"only {len(players)} players returned ({len(failed)} team call(s) failed)")
        out = {"fetched_at": now, "error": None, "failed_teams": failed, "players": players}
        PATH.write_text(json.dumps(out, indent=1))
        return out
    except Exception as e:  # noqa: BLE001
        prev = load()
        prev["error"] = f"{errs.short(e)} (using copy from {prev.get('fetched_at') or 'never'})"
        return prev


def load() -> dict:
    try:
        return json.loads(PATH.read_text())
    except Exception:  # noqa: BLE001
        return {"fetched_at": None, "error": "no roster pulled yet", "players": []}
