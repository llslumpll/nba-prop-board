"""
Injury report from ESPN's public site API (no key):
  https://site.api.espn.com/apis/site/v2/sports/basketball/nba/injuries

Shape (checked Oct 16, 2026): {"injuries": [{"displayName": team,
"injuries": [{"status": "Out" | "Day-To-Day" | ..., "date",
"shortComment", "athlete": {"displayName", "team": {"abbreviation"}}}]}]}

Fail-soft: on any failure the last good copy in data/injuries.json is
kept and its age is shown on the page. A failed fetch never wipes it.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import requests

import errs

import teams

URL = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/injuries"
PATH = Path(__file__).parent.parent / "data" / "injuries.json"

# How the model treats each status. "out" removes the player and frees his
# minutes for teammates; "doubtful" is treated as out; "questionable" and
# "day-to-day" keep him in but are flagged and shown in the "why" text.
STATUS_CLASS = {
    "out": "out", "doubtful": "out", "suspension": "out", "suspended": "out",
    "questionable": "questionable", "day-to-day": "questionable", "game time decision": "questionable",
    "probable": "probable",
}


def fetch() -> dict:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    try:
        r = requests.get(URL, timeout=20, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        raw = r.json()
        players = []
        for team_block in raw.get("injuries") or []:
            for inj in team_block.get("injuries") or []:
                ath = inj.get("athlete") or {}
                team = teams.canon(((ath.get("team") or {}).get("abbreviation")), "espn")
                status = str(inj.get("status") or "").strip()
                players.append({
                    "player": ath.get("displayName"),
                    "team": team,
                    "status": status,
                    "class": STATUS_CLASS.get(status.lower(), "questionable" if status else None),
                    "date": inj.get("date"),
                    "comment": (inj.get("shortComment") or "")[:240],
                })
        if not players:
            raise ValueError("response parsed but held no injury entries")
        out = {"fetched_at": now, "error": None, "players": players}
        PATH.write_text(json.dumps(out, indent=2))
        return out
    except Exception as e:  # noqa: BLE001
        prev = load()
        prev["error"] = f"{errs.short(e)} (showing copy from {prev.get('fetched_at') or 'never'})"
        return prev


def load() -> dict:
    try:
        return json.loads(PATH.read_text())
    except Exception:
        return {"fetched_at": None, "error": "no injury report fetched yet", "players": []}


def by_player(report: dict | None = None) -> dict:
    report = report or load()
    return {p["player"]: p for p in report.get("players", []) if p.get("player")}
