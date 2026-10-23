"""
FanDuel lines via The Odds API (licensed aggregator), sport basketball_nba.

Free plan: 500 credits/month. Cost = markets x regions; bookmakers=fanduel
counts as one region; /events is free; empty responses aren't charged; a
per-event call is charged only for markets actually returned.

Budget (see README for the month-by-month math):
  game lines   h2h + spreads + totals, one bulk call      = 3 credits/day
  points props player_points, one call per game tipping within
               PROPS_WINDOW_HOURS                         = 1 credit/game
  busiest month (Jan 2027, 236 games): 93 + 236 = ~329 credits

Guards carried over from the NFL site:
  * RESERVE_CREDITS: the props loop pulls fewer games (soonest first) rather
    than dip under the reserve, using the API's own x-requests-remaining.
  * a failed or empty pull never overwrites the last good cache (build.py).
  * the key is only ever read from the ODDS_API_KEY env var and is scrubbed
    from every message before anything is logged or saved.
  * if the game-lines call fails, the props pull is skipped.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

import errs

import teams

BASE_URL = "https://api.the-odds-api.com/v4"
SPORT = "basketball_nba"
BOOKMAKER = "fanduel"
DATA_DIR = Path(__file__).parent.parent / "data"
USAGE_LOG_PATH = DATA_DIR / "oddsapi_usage_log.json"

# Plan switch: the ODDS_PLAN repository variable (Settings -> Secrets and variables -> Actions -> Variables).
# Unset / "free": 500 credits a month -> points props once a day, game lines twice a day.
# "20k" (or any paid plan): points, rebounds, assists and threes props + game lines on EVERY pull
# (trigger the FanDuel workflow hourly); RESERVE_CREDITS still stops it before the plan runs out.
PLAN = (os.environ.get("ODDS_PLAN") or "free").strip().lower()
PAID = PLAN not in ("", "free")
PROP_MARKETS = ({"player_points": "pts", "player_rebounds": "reb", "player_assists": "ast", "player_threes": "fg3m"}
                if PAID else {"player_points": "pts"})
GAME_MARKETS = "h2h,spreads,totals"
RESERVE_CREDITS = 60
PROPS_WINDOW_HOURS = 14
REQUEST_TIMEOUT = 25


def _now():
    return datetime.now(timezone.utc)


def _api_key():
    raw = os.environ.get("ODDS_API_KEY") or ""
    return raw.strip().strip("'\"").strip() or None


def _clean(text) -> str:
    msg = str(text)
    key = _api_key()
    if key:
        msg = msg.replace(key, "***")
    return re.sub(r"(?i)(api_?key=)[^&\s'\"]+", r"\1***", msg)


class OddsApiError(Exception):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


def _get(path, params=None):
    key = _api_key()
    if not key:
        raise OddsApiError("ODDS_API_KEY is not set")
    q = dict(params or {})
    q["apiKey"] = key
    try:
        r = requests.get(f"{BASE_URL}{path}", params=q, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        return r.json(), r.headers
    except Exception as e:  # noqa: BLE001
        resp = getattr(e, "response", None)
        status = getattr(resp, "status_code", None)
        detail = ""
        if resp is not None:
            try:
                body = resp.json()
                bits = [str(body[k]) for k in ("error_code", "message") if isinstance(body, dict) and body.get(k)]
                detail = (" | API says: " + " - ".join(bits)) if bits else ""
            except Exception:  # noqa: BLE001
                pass
        raise OddsApiError(_clean(f"{errs.short(e)}{detail}"),
                           status) from None


def _int(v):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def _log_usage(label, headers) -> dict:
    snap = {"call": label, "requests_remaining": _int(headers.get("x-requests-remaining")),
            "requests_used": _int(headers.get("x-requests-used")), "requests_last": _int(headers.get("x-requests-last")),
            "logged_at": _now().strftime("%Y-%m-%d %H:%M UTC")}
    try:
        log = json.loads(USAGE_LOG_PATH.read_text()) if USAGE_LOG_PATH.exists() else {"entries": []}
    except Exception:  # noqa: BLE001
        log = {"entries": []}
    log["entries"] = (log.get("entries", []) + [snap])[-300:]
    try:
        USAGE_LOG_PATH.write_text(json.dumps(log, indent=2))
    except Exception:  # noqa: BLE001
        pass
    return snap


def current_usage() -> dict | None:
    try:
        e = json.loads(USAGE_LOG_PATH.read_text()).get("entries", [])
        return e[-1] if e else None
    except Exception:  # noqa: BLE001
        return None


# --- odds math --------------------------------------------------------------

def american_to_prob(price):
    if price is None or price == 0:
        return None
    return 100 / (price + 100) if price > 0 else -price / (-price + 100)


def no_vig_pair(a, b):
    pa, pb = american_to_prob(a), american_to_prob(b)
    if pa is None or pb is None:
        return None, None
    return pa / (pa + pb), pb / (pa + pb)


def fmt_price(p):
    if p is None:
        return "--"
    return f"+{int(p)}" if p > 0 else f"{int(p)}"


def _t(s):
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except Exception:  # noqa: BLE001
        return None


# --- game lines ---------------------------------------------------------------

def _distill(raw):
    games = []
    for g in raw:
        home = teams.canon(g.get("home_team"), "oddsapi")
        away = teams.canon(g.get("away_team"), "oddsapi")
        e = {"id": g.get("id"), "home": home, "away": away, "commence_time": g.get("commence_time"),
             "home_name": g.get("home_team"), "away_name": g.get("away_team")}
        has = False
        for book in g.get("bookmakers", []):
            if book.get("key") != BOOKMAKER:
                continue
            for m in book.get("markets", []):
                oc = m.get("outcomes", [])
                if m.get("key") == "h2h":
                    pr = {o.get("name"): o.get("price") for o in oc}
                    e["home_price"], e["away_price"] = pr.get(g.get("home_team")), pr.get(g.get("away_team"))
                    has = True
                elif m.get("key") == "spreads":
                    pt = {o.get("name"): (o.get("point"), o.get("price")) for o in oc}
                    hp, ap = pt.get(g.get("home_team"), (None, None)), pt.get(g.get("away_team"), (None, None))
                    e["home_spread"], e["home_spread_price"] = hp
                    e["away_spread"], e["away_spread_price"] = ap
                    has = True
                elif m.get("key") == "totals":
                    for o in oc:
                        side = str(o.get("name", "")).lower()
                        if side in ("over", "under"):
                            e["total"] = o.get("point")
                            e[f"{side}_price"] = o.get("price")
                            has = True
        if has:
            games.append(e)
    return games


def fetch_game_lines() -> dict:
    res = {"games": [], "error": None, "pulled_at": _now().strftime("%Y-%m-%d %H:%M UTC"), "diagnostics": {}}
    try:
        raw, h = _get(f"/sports/{SPORT}/odds", {"bookmakers": BOOKMAKER, "markets": GAME_MARKETS,
                                                "oddsFormat": "american"})
        res["diagnostics"]["usage"] = _log_usage("game_lines", h)
    except OddsApiError as e:
        res["error"] = str(e)
        return res
    res["games"] = _distill(raw)
    res["diagnostics"]["games_returned"] = len(raw)
    res["diagnostics"]["games_with_fanduel"] = len(res["games"])
    return res


# --- player props -------------------------------------------------------------

def fetch_props(remaining_credits: int | None) -> dict:
    res = {"lines": [], "error": None, "pulled_at": _now().strftime("%Y-%m-%d %H:%M UTC"), "diagnostics": {}}
    d = res["diagnostics"]
    try:
        events, h = _get(f"/sports/{SPORT}/events", {})
        _log_usage("events (free)", h)
    except OddsApiError as e:
        res["error"] = str(e)
        return res
    now = _now()
    window = [(t, ev) for ev in events if (t := _t(ev.get("commence_time"))) and now < t <= now + timedelta(hours=PROPS_WINDOW_HOURS)]
    window.sort(key=lambda x: x[0])
    d["events_listed"], d["events_in_window"] = len(events), len(window)
    per_game = len(PROP_MARKETS)
    if remaining_credits is not None:
        affordable = max(0, (remaining_credits - RESERVE_CREDITS) // per_game)
        d["credits_remaining_before"] = remaining_credits
        if affordable < len(window):
            d["budget_limited"] = True
            window = window[:affordable]
    d["events_attempted"] = len(window)
    failures, with_lines = [], 0
    for t, ev in window:
        try:
            data, h = _get(f"/sports/{SPORT}/events/{ev['id']}/odds",
                           {"bookmakers": BOOKMAKER, "markets": ",".join(PROP_MARKETS), "oddsFormat": "american"})
            d["usage"] = _log_usage(f"props:{ev['id']}", h)
        except OddsApiError as e:
            failures.append(str(e))
            continue
        got = 0
        for book in data.get("bookmakers", []):
            if book.get("key") != BOOKMAKER:
                continue
            for m in book.get("markets", []):
                stat = PROP_MARKETS.get(m.get("key"))
                if not stat:
                    continue
                byp: dict = {}
                for o in m.get("outcomes", []):
                    who, pt = o.get("description"), o.get("point")
                    if not who or pt is None:
                        continue
                    slot = byp.setdefault(who, {"line": pt})
                    side = str(o.get("name", "")).lower()
                    if side in ("over", "under"):
                        slot[f"{side}_price"] = o.get("price")
                for who, s in byp.items():
                    res["lines"].append({"player": who, "stat": stat, "line": s["line"],
                                         "over_price": s.get("over_price"), "under_price": s.get("under_price"),
                                         "home": teams.canon(ev.get("home_team"), "oddsapi"),
                                         "away": teams.canon(ev.get("away_team"), "oddsapi"),
                                         "commence_time": ev.get("commence_time")})
                    got += 1
        with_lines += bool(got)
    d["events_with_lines"], d["events_failed"], d["lines_found"] = with_lines, len(failures), len(res["lines"])
    if failures:
        d["sample_errors"] = sorted(set(failures))[:3]
    if window and len(failures) == len(window):
        res["error"] = f"all {len(window)} per-game calls failed: {failures[0]}"
    return res


# --- helpers for build.py --------------------------------------------------------

def upcoming_only(items, now=None):
    now = now or _now()
    return [it for it in items if (t := _t(it.get("commence_time"))) is None or t > now]


def game_odds(games, away, home) -> dict | None:
    g = next((g for g in games if g.get("home") == home and g.get("away") == away), None)
    if not g:
        return None
    out = {"commence_time": g.get("commence_time")}
    if g.get("home_price") is not None and g.get("away_price") is not None:
        ph, pa = no_vig_pair(g["home_price"], g["away_price"])
        out["moneyline"] = {"home_price": g["home_price"], "away_price": g["away_price"],
                            "home_str": fmt_price(g["home_price"]), "away_str": fmt_price(g["away_price"]),
                            "home_prob": round(ph * 100, 1), "away_prob": round(pa * 100, 1)}
    if g.get("home_spread") is not None:
        out["spread"] = {"home_point": g["home_spread"], "away_point": g.get("away_spread"),
                         "home_price": g.get("home_spread_price"), "away_price": g.get("away_spread_price"),
                         "home_price_str": fmt_price(g.get("home_spread_price")),
                         "away_price_str": fmt_price(g.get("away_spread_price"))}
    if g.get("total") is not None:
        out["total"] = {"point": g["total"], "over_price": g.get("over_price"), "under_price": g.get("under_price"),
                        "over_str": fmt_price(g.get("over_price")),
                        "under_str": fmt_price(g.get("under_price"))}
    return out if len(out) > 1 else None
