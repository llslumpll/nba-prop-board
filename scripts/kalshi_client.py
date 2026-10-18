"""
Kalshi public market data (no key): https://api.elections.kalshi.com/trade-api/v2

Series checked Oct 16, 2026 via /series/{ticker} (a made-up ticker returns an
error, these return a record):
  KXNBAGAME  NBA Game (moneyline)       31 open events that day
  KXNBASPREAD  NBA Spread               KXNBATOTAL  NBA Total
  KXNBATEAMTOTAL  NBA Team Total        KXNBAPTS  NBA Player Points
  KXNBAREB  Player Rebounds             KXNBAAST  Player Assists
  KXNBA3PT  Player Threes               KXNBAPRA  Player Pts+Reb+Ast
Player-prop series had 0 open events before the season; they list on game
days. Every build records per-series counts in data/kalshi_raw.json, so a
series that stops returning markets shows up there.

Tickers: event KXNBAGAME-26OCT20BOSDET = away BOS at home DET.
Player props are "N+" ladders, e.g. KXNBAPTS-26JUN13NYKSAS-NYKKTOWNS32-10 is
"10+ points". A ladder rung N is the same bet as OVER N-0.5, so each rung
becomes one half-point line with its own price.

Caps are applied PER SERIES (sorted by volume), never across a bucket: on
the NFL site a shared cap let spreads/totals push the moneylines off the page.

Prices: midpoint of yes bid/ask is the market's probability. The price to
actually take a side is the ask (YES) or 1 - bid (NO); those are what the
break-even uses.

Fails soft: any error is recorded and the site builds without Kalshi.
"""

from __future__ import annotations

import re
import time

import requests

import errs

import teams

BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"

SERIES = {
    "KXNBAGAME": ("game", "moneyline"),
    "KXNBASPREAD": ("game", "spread"),
    "KXNBATOTAL": ("game", "total"),
    "KXNBATEAMTOTAL": ("game", "team_total"),
    "KXNBAPTS": ("player", "pts"),
    "KXNBAREB": ("player", "reb"),
    "KXNBAAST": ("player", "ast"),
    "KXNBA3PT": ("player", "fg3m"),
    "KXNBAPRA": ("player", "pra"),
}
CAP_PER_SERIES = 400
MAX_SPREAD = 0.10   # widest bid/ask gap still treated as a real price
_SLEEP = 0.25


def _get(path, params=None, timeout=15):
    """GET with a polite retry: Kalshi answers HTTP 429 when asked too fast
    (seen on the first Actions run). Waits Retry-After or 1.5s, 3s, 6s."""
    for attempt in range(4):
        r = requests.get(f"{BASE_URL}{path}", params=params, timeout=timeout)
        if r.status_code == 429 and attempt < 3:
            try:
                wait = float(r.headers.get("Retry-After") or 0)
            except ValueError:
                wait = 0
            time.sleep(max(wait, 1.5 * 2 ** attempt))
            continue
        r.raise_for_status()
        return r.json()


def _events(series):
    out, cursor = [], None
    for _ in range(10):
        params = {"series_ticker": series, "status": "open", "limit": 200}
        if cursor:
            params["cursor"] = cursor
        d = _get("/events", params)
        batch = d.get("events") or []
        out += batch
        cursor = d.get("cursor")
        if not cursor or not batch:
            break
    return out


def _num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _price_fields(m):
    # Newer responses carry *_dollars strings; older ones carry cents ints.
    bid = _num(m.get("yes_bid_dollars"))
    ask = _num(m.get("yes_ask_dollars"))
    if bid is None and m.get("yes_bid") is not None:
        bid = _num(m.get("yes_bid")) / 100
    if ask is None and m.get("yes_ask") is not None:
        ask = _num(m.get("yes_ask")) / 100
    bid = bid if bid and bid > 0 else None
    ask = ask if ask and ask < 1 else None
    if bid and ask and ask - bid <= MAX_SPREAD:
        mid = (bid + ask) / 2
    else:
        # One-sided, or so wide it isn't a price (Kalshi lists player
        # ladders days early at e.g. 6c bid / 94c ask, whose midpoint is a
        # meaningless 50%). No fair price, never guessed.
        mid = None
    return bid, ask, mid


_EVENT_RE = re.compile(r"-(\d{2})([A-Z]{3})(\d{2})([A-Z]{2,4})([A-Z]{2,4})$")


def parse_event_teams(event_ticker: str):
    """'KXNBAGAME-26OCT20BOSDET' -> ('BOS', 'DET', '2026-10-20'). Team codes
    are 3 letters on every ticker seen so far; the split tries 3+3 first."""
    m = re.search(r"-(\d{2})([A-Z]{3})(\d{2})([A-Z]+)$", event_ticker or "")
    if not m:
        return None
    yy, mon, dd, codes = m.groups()
    months = "JAN FEB MAR APR MAY JUN JUL AUG SEP OCT NOV DEC".split()
    if mon not in months:
        return None
    date = f"20{yy}-{months.index(mon) + 1:02d}-{dd}"
    for split in (3, 2, 4):
        a, h = codes[:split], codes[split:]
        ca, ch = teams.canon(a, "kalshi") if a else None, teams.canon(h, "kalshi") if h else None
        if ca and ch:
            return ca, ch, date
    teams.note_unmatched("kalshi", codes)
    return None


_LADDER_RE = re.compile(r"^(?P<name>.+?)(?::|\s+-)?\s+(?P<n>\d+)\+")


def _player_and_threshold(m):
    """Player name and N from an 'N+' rung. Uses floor_strike when Kalshi
    sends it; otherwise reads the market title ('Karl-Anthony Towns: 10+
    points'). Returns (name, N) or (None, None) -- unparsed rungs are
    counted in diagnostics, never guessed."""
    n = None
    fs = _num(m.get("floor_strike"))
    if fs is not None:
        n = int(fs) if float(fs).is_integer() else int(fs + 0.5)
    tail = (m.get("ticker") or "").rsplit("-", 1)[-1]
    if n is None and tail.isdigit():
        n = int(tail)
    name = None
    for field in ("yes_sub_title", "title", "subtitle"):
        txt = (m.get(field) or "").strip()
        mm = _LADDER_RE.match(txt)
        if mm:
            name = re.sub(r"^will\s+", "", mm.group("name").strip(), flags=re.I)
            name = re.sub(r"\s+(score|scores|record|records|have|has|make|makes|get|gets|grab|hit|hits)$",
                          "", name, flags=re.I).strip()
            if n is None:
                n = int(mm.group("n"))
            break
    return name, n


def fetch_nba() -> dict:
    """Returns {"game": [...], "player": [...], "error", "diagnostics"}."""
    res = {"game": [], "player": [], "error": None,
           "diagnostics": {"per_series": {}, "samples": {}, "unparsed_player_rungs": 0}}
    any_ok, last_err = False, None
    for series, (bucket, kind) in SERIES.items():
        try:
            events = _events(series)
        except Exception as e:  # noqa: BLE001
            last_err = errs.short(e)
            res["diagnostics"]["per_series"][series] = f"error: {last_err}"
            if isinstance(e, requests.ConnectionError) and not any_ok:
                break  # host unreachable: the other series would fail the same way
            continue
        any_ok = True
        rows = []
        time.sleep(_SLEEP)
        for ev in events:
            et = ev.get("event_ticker")
            if not et:
                continue
            parsed = parse_event_teams(et)
            try:
                markets = _get("/markets", {"event_ticker": et, "status": "open", "limit": 1000}).get("markets") or []
            except Exception:  # noqa: BLE001
                continue
            time.sleep(_SLEEP)
            for m in markets:
                bid, ask, mid = _price_fields(m)
                rec = {
                    "series": series, "kind": kind, "event_ticker": et, "ticker": m.get("ticker"),
                    "title": m.get("title"), "sub_title": m.get("yes_sub_title"),
                    "away": parsed[0] if parsed else None, "home": parsed[1] if parsed else None,
                    "event_date": parsed[2] if parsed else None,
                    "yes_bid": bid, "yes_ask": ask, "mid": mid,
                    "floor_strike": _num(m.get("floor_strike")),
                    "volume": m.get("volume") or 0,
                    "close_time": m.get("close_time") or ev.get("close_time"),
                }
                if bucket == "player":
                    name, n = _player_and_threshold(m)
                    if not name or n is None:
                        res["diagnostics"]["unparsed_player_rungs"] += 1
                        continue
                    rec["player"], rec["threshold"] = name, n
                    rec["line"] = n - 0.5  # "N+" == OVER N-0.5
                else:
                    rec["team_suffix"] = (m.get("ticker") or "").rsplit("-", 1)[-1]
                rows.append(rec)
            if events and series not in res["diagnostics"]["samples"] and markets:
                m0 = markets[0]
                res["diagnostics"]["samples"][series] = {k: m0.get(k) for k in (
                    "ticker", "title", "yes_sub_title", "subtitle", "floor_strike", "yes_bid_dollars", "yes_ask_dollars")}
        rows.sort(key=lambda r: (r["mid"] is not None, r["volume"]), reverse=True)
        kept = rows[:CAP_PER_SERIES]
        res[bucket] += kept
        res["diagnostics"]["per_series"][series] = (
            f"{len(events)} event(s), {len(rows)} market(s), kept {len(kept)}, "
            f"{sum(1 for r in kept if r['mid'] is not None)} priced (bid/ask within {MAX_SPREAD * 100:.0f}c)")
    if not any_ok and last_err:
        res["error"] = last_err
    return res


# ---------------------------------------------------------------------------
# Helpers for build.py
# ---------------------------------------------------------------------------

def moneyline(kalshi: dict, away: str, home: str, date: str | None = None) -> dict | None:
    """Both sides of KXNBAGAME for one game, or None. Each side's chance is
    its own midpoint; with both quoted they're normalised to sum to 100."""
    sides = {}
    for r in kalshi.get("game", []):
        if r["kind"] != "moneyline" or r["away"] != away or r["home"] != home:
            continue
        if date and r.get("event_date") and r["event_date"] != date:
            continue
        code = teams.canon(r.get("team_suffix"), "kalshi")
        if code == home:
            sides["home"] = r
        elif code == away:
            sides["away"] = r
    if not sides:
        return None
    h, a = sides.get("home"), sides.get("away")
    hp, ap = (h or {}).get("mid"), (a or {}).get("mid")
    if hp is not None and ap is not None and hp + ap > 0:
        hp, ap = hp / (hp + ap), ap / (hp + ap)
    return {"home_prob": round(hp * 100, 1) if hp is not None else None,
            "away_prob": round(ap * 100, 1) if ap is not None else None,
            "home_ask": (h or {}).get("yes_ask"), "away_ask": (a or {}).get("yes_ask"),
            "volume": sum((x or {}).get("volume", 0) for x in (h, a))}


def player_ladders(kalshi: dict) -> dict:
    """{(player, stat, away, home): [{threshold, line, mid, yes_bid, yes_ask, volume}, ...]}"""
    out: dict = {}
    for r in kalshi.get("player", []):
        key = (r["player"], r["kind"], r["away"], r["home"])
        out.setdefault(key, []).append({k: r[k] for k in ("threshold", "line", "mid", "yes_bid", "yes_ask", "volume")})
    for v in out.values():
        v.sort(key=lambda x: x["threshold"])
    return out


MAIN_LINE_RANGE = (0.25, 0.80)  # a rung only counts as a line when its OVER price is in here


def main_line(rungs: list[dict]) -> dict | None:
    """The rung priced closest to 50% -- the ladder's equivalent of a book's
    main line. Only priced rungs whose OVER chance is within MAIN_LINE_RANGE
    count: early in the week Kalshi often has firm prices only on lopsided
    rungs (e.g. "8+ assists" at 6%), which aren't real lines to pick against.
    None when no rung qualifies."""
    lo, hi = MAIN_LINE_RANGE
    quoted = [r for r in rungs if r.get("mid") is not None and lo <= r["mid"] <= hi]
    if not quoted:
        return None
    return min(quoted, key=lambda r: abs(r["mid"] - 0.5))
