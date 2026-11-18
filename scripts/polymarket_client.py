"""Polymarket US prices (update 79).

The user trades on Polymarket US (polymarket.us), which has its own order book (it differs from international
Polymarket by a cent or two). Its public gateway needs no key:

    GET https://gateway.polymarket.us/v1/events/slug/nba-{away}-{home}-{YYYY-MM-DD}

Each game event lists ~700 markets. We keep the full-game moneyline, full-game spreads and totals, and the player
"N+" ladders (points, rebounds, assists, threes, PRA), each with the best bid/ask. What a buy really costs:
YES = ask, NO = 1 - bid, plus the taker fee Polymarket reports per market (feeCoefficient x p x (1 - p)).
Markets whose bid/ask are more than 10 cents apart (or have no bid) are skipped: those prices aren't real.

data/polymarket.json  {"at", "date", "error", "games": {game_id: {"slug", "ml": {TEAM: cost},
                       "spreads": [[TEAM, line, cost]], "totals": [[line, over_cost, under_cost]],
                       "props": {"normname|stat": [[n, bid, ask, fee]]}}}}
"""
from __future__ import annotations

import json
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

import requests

BASE = "https://gateway.polymarket.us"
FILE = Path(__file__).resolve().parent.parent / "data" / "polymarket.json"
MAX_SPREAD = 0.10
# site team code -> Polymarket US slug codes to try (checked Nov 18 2026: PHX=pho, SAS=sa, NOP=no, GSW=gs)
ALT = {"PHX": ["pho", "phx"], "SAS": ["sa", "sas"], "NOP": ["no", "nop"], "GSW": ["gs", "gsw"], "NYK": ["ny", "nyk"],
       "UTA": ["uta", "utah"], "WAS": ["was", "wsh"], "BKN": ["bkn", "bk"]}
REV = {"GS": "GSW", "SA": "SAS", "NO": "NOP", "PHO": "PHX", "NY": "NYK", "UTAH": "UTA", "WSH": "WAS", "BK": "BKN"}


def _ab(side) -> str:
    return (((side or {}).get("team") or {}).get("abbreviation") or "").upper()


STAT = {"basketball_player_points": "pts", "basketball_player_rebounds": "reb", "basketball_player_assists": "ast",
        "basketball_player_threes": "fg3m", "basketball_player_points_rebounds_assists": "pra"}
_cache = None


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    for ch in ".'-":
        s = s.replace(ch, "" if ch != "-" else " ")
    return " ".join(w for w in s.split() if w not in ("jr", "sr", "ii", "iii", "iv"))


def _q(m: dict, k: str) -> float | None:
    v = (m.get(k) or {}).get("value")
    try:
        return float(v) if v is not None else None
    except ValueError:
        return None


def cost(p: float | None, fee: float) -> float | None:
    if p is None or p <= 0 or p >= 1:
        return None
    return round(min(0.995, p + fee * p * (1 - p)), 4)


def _get(slug: str):
    for attempt in range(2):
        try:
            r = requests.get(f"{BASE}/v1/events/slug/{slug}", timeout=20)
            if r.status_code == 404:
                return None
            if r.status_code == 429 and attempt == 0:
                time.sleep(2)
                continue
            r.raise_for_status()
            return r.json().get("event")
        except requests.RequestException:
            if attempt:
                raise
            time.sleep(1)
    return None


def parse(ev: dict) -> dict:
    out = {"slug": ev.get("slug"), "ml": {}, "spreads": [], "totals": [], "props": {}}
    for m in ev.get("markets") or []:
        if m.get("closed") or not m.get("active") or m.get("hidden"):
            continue
        smt = m.get("sportsMarketType") or ""
        bid, ask = _q(m, "bestBidQuote"), _q(m, "bestAskQuote")
        if bid is None or ask is None or bid <= 0 or ask - bid > MAX_SPREAD + 1e-9:
            continue
        fee = float(m.get("feeCoefficient") or 0)
        sides = m.get("marketSides") or []
        long_ = next((s for s in sides if s.get("long")), None)
        short = next((s for s in sides if not s.get("long")), None)
        abbr = lambda s: REV.get(_ab(s), _ab(s))  # noqa: E731
        if smt == "basketball_team_full_game_winner" and long_ and short:
            out["ml"][abbr(long_)] = cost(ask, fee)
            out["ml"][abbr(short)] = cost(1 - bid, fee)
        elif smt == "basketball_team_full_game_spread" and long_ and short and m.get("line") is not None:
            # long side = its description's number (e.g. "+38.50" for the away team); short side is the other team
            try:
                ln = float(long_.get("description"))
            except (TypeError, ValueError):
                continue
            out["spreads"].append([abbr(long_), ln, cost(ask, fee)])
            out["spreads"].append([abbr(short), -ln, cost(1 - bid, fee)])
        elif smt == "basketball_team_full_game_total" and m.get("line") is not None:
            out["totals"].append([float(m["line"]), cost(ask, fee), cost(1 - bid, fee)])
        elif smt in STAT and m.get("line") is not None:
            title = m.get("title") or ""
            n = int(m["line"])
            name = title.split(f" {n}+")[0].strip()
            if not name or name == title:
                continue
            out["props"].setdefault(f"{_norm(name)}|{STAT[smt]}", []).append([n, bid, ask, fee])
    for k in out["props"]:
        out["props"][k].sort()
    return out


def fetch(sl: dict, now: datetime | None = None) -> dict:
    """Pull every game on the slate and save data/polymarket.json. Never raises."""
    now = now or datetime.now(timezone.utc)
    date = (sl or {}).get("date")
    res = {"at": now.strftime("%Y-%m-%d %H:%M UTC"), "date": date, "error": None, "games": {}, "missing": []}
    if not date:
        return res
    try:
        for g in sl.get("games") or []:
            ev = None
            for a in ALT.get(g["away"], [g["away"].lower()]):
                for h in ALT.get(g["home"], [g["home"].lower()]):
                    ev = _get(f"nba-{a}-{h}-{date}")
                    if ev:
                        break
                if ev:
                    break
            if not ev:
                res["missing"].append(f"{g['away']}@{g['home']}")
                continue
            res["games"][str(g["game_id"])] = parse(ev)
    except Exception as e:  # noqa: BLE001
        res["error"] = f"{type(e).__name__}: {e}"[:300]
    if res["games"] or not FILE.exists():
        FILE.write_text(json.dumps(res, indent=1, sort_keys=True))
    global _cache
    _cache = res
    return res


def load() -> dict:
    global _cache
    if _cache is None:
        try:
            _cache = json.loads(FILE.read_text())
        except Exception:  # noqa: BLE001
            _cache = {"games": {}}
    return _cache


def prop_cost(player: str, stat: str, line: float, over: bool, game_id=None, date: str | None = None) -> float | None:
    """What Polymarket US charges for OVER/UNDER `line` (an x.5 number) on this player's stat, fee included."""
    d = load()
    if date and d.get("date") != date:
        return None
    if line is None or line % 1 != 0.5:
        return None
    n = int(line + 0.5)
    games = [d["games"].get(str(game_id))] if game_id is not None else list(d.get("games", {}).values())
    for g in games:
        lad = (g or {}).get("props", {}).get(f"{_norm(player)}|{stat}")
        for rung, bid, ask, fee in lad or []:
            if rung == n:
                return cost(ask, fee) if over else cost(1 - bid, fee)
    return None


def spread_cost(team: str, line: float, date: str | None = None) -> float | None:
    d = load()
    if date and d.get("date") != date:
        return None
    for g in d.get("games", {}).values():
        for t, ln, c in g.get("spreads") or []:
            if t == team and abs(ln - line) < 1e-9:
                return c
    return None


def ml_cost(team: str, game_id=None) -> float | None:
    g = load().get("games", {}).get(str(game_id)) if game_id is not None else None
    return (g or {}).get("ml", {}).get(team)
