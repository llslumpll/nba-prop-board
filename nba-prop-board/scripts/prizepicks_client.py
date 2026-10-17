"""
PrizePicks NBA lines -- EXPERIMENTAL.

PrizePicks has no official API. partner-api.prizepicks.com is unofficial and
can change or block cloud IPs without notice; the NFL site has read it from
GitHub Actions without trouble so far. Pages label these lines experimental.

The NBA league is found by name from /leagues (exact "NBA"; on Oct 16, 2026
that was id 7, alongside NBA1H/NBA2H/NBA1Q/NBAP/... which are excluded).
Only standard lines are kept: "goblin"/"demon" alternates are dropped.

PrizePicks lines carry no price. The break-even used on History assumes a
6-pick flex entry (54.2% per pick); other entry types differ and the page
says so.

Fails soft: errors are returned, never raised; nothing is invented.
"""

from __future__ import annotations

import requests

import errs

BASE_URL = "https://partner-api.prizepicks.com"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json",
}
BREAK_EVEN_PCT = 54.2  # 6-pick flex, per pick
LEAGUE_NAME = "NBA"

# PrizePicks stat_type (lowercased) -> site stat. Exact matches only, so
# "Points (Combo)" or "Fantasy Score" never slip in as plain points.
STAT_MAP = {
    "points": "pts", "rebounds": "reb", "assists": "ast",
    "3-pt made": "fg3m", "3-pointers made": "fg3m", "3pt made": "fg3m",
    "pts+rebs+asts": "pra", "pts+rebs": "pr", "pts+asts": "pa", "rebs+asts": "ra",
}


def _get(path, params=None, timeout=20):
    r = requests.get(f"{BASE_URL}{path}", params=params, headers=HEADERS, timeout=timeout)
    r.raise_for_status()
    return r.json()


def fetch_nba() -> dict:
    """{"lines": [{player, team, stat, line, start_time, opponent}], "error", "diagnostics"}"""
    res = {"lines": [], "error": None, "diagnostics": {}}
    try:
        leagues = _get("/leagues").get("data") or []
        lid = next((lg["id"] for lg in leagues
                    if ((lg.get("attributes") or {}).get("name") or "").strip().upper() == LEAGUE_NAME), None)
        res["diagnostics"]["league_id"] = lid
        if lid is None:
            res["error"] = f"no league named {LEAGUE_NAME!r} in /leagues ({len(leagues)} leagues)"
            return res
        payload = _get("/projections", {"league_id": lid, "per_page": 2000, "single_stat": "true"})
        players = {it["id"]: it.get("attributes") or {} for it in payload.get("included") or []
                   if it.get("type") == "new_player"}
        seen_types, alt = set(), 0
        for pr in payload.get("data") or []:
            a = pr.get("attributes") or {}
            st = (a.get("stat_type") or "").strip().lower()
            seen_types.add(st)
            if str(a.get("odds_type") or "standard").lower() not in ("standard", ""):
                alt += 1
                continue
            stat = STAT_MAP.get(st)
            pid = ((pr.get("relationships") or {}).get("new_player") or {}).get("data") or {}
            pa = players.get(pid.get("id")) or {}
            if not stat or not pa.get("name") or a.get("line_score") is None:
                continue
            res["lines"].append({
                "player": pa.get("name"), "team": pa.get("team"), "stat": stat,
                "line": float(a["line_score"]), "start_time": a.get("start_time"),
                "opponent": a.get("description"),
            })
        d = res["diagnostics"]
        d["projections_returned"] = len(payload.get("data") or [])
        d["lines_kept"] = len(res["lines"])
        d["alternate_lines_dropped"] = alt
        d["stat_types_seen"] = sorted(seen_types)[:40]
    except Exception as e:  # noqa: BLE001
        res["error"] = errs.short(e)
    return res
