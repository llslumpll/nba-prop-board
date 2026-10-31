"""
Market reading (update 48): learn which lines and prices can be trusted, from graded results.

1) KALSHI LAGGING THE BOOKS (tracking, earns a stake). Kalshi's main line for a player prop is the rung priced
   nearest 50%. When PrizePicks and FanDuel both have the line 1+ points away from it on the same side, the
   bigger books are usually right: betting Kalshi's rung toward their number hit 62.5% over 849 graded picks
   (Oct 20-30, both sides, 10 of 11 days above 55%). What that doesn't show is the price you could actually get
   (some "nearest 50%" rungs are thin or far from even money), so every lag is recorded here with the real
   Kalshi cost at that moment (ask for YES, 1 - bid for NO, fee included), kept fresh until tip, and graded.
   It carries no stake until it earns one: LAG_EARN_N graded and the cautious end of its hit rate (95% Wilson
   low) above the average cost it paid.

2) WHICH LINES TO TRUST: every graded pick by source (and Kalshi price band): hit rate vs what the price needed,
   flat units, and the cautious end. A source is TRUSTED only when even the cautious end beats what it needs.

3) Every recorded pick also keeps each book's number and Kalshi's ladder (bid / ask per rung) at freeze
   (predictions: "mkt", "kladder"), so later market ideas can be tested with real prices.

data/market/DATE.json  {"gid|player|stat": lag record}
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from pathlib import Path

import kalshi_client
import predictions
import proven

DIR = Path(__file__).parent.parent / "data" / "market"
LAG_MIN = 1.0          # points between Kalshi's even-money rung and the books' line
LAG_EARN_N = 100       # graded lag bets before they can earn a stake
LAG_UNITS = 0.5
BOOKS = ("prizepicks", "fanduel")


def _load(date):
    try:
        return json.loads((DIR / f"{date}.json").read_text())
    except Exception:  # noqa: BLE001
        return {}


def _save(date, d):
    DIR.mkdir(parents=True, exist_ok=True)
    (DIR / f"{date}.json").write_text(json.dumps(d, indent=1, sort_keys=True))


def all_dates():
    return sorted(p.stem for p in DIR.glob("*.json")) if DIR.exists() else []


def find_lags(line_book: dict, sl: dict) -> list[dict]:
    """Kalshi's even-money rung vs the books' line, for games that haven't started."""
    games = {g["game_id"]: g for g in sl.get("games", []) if not g.get("started")}
    out = []
    for (player, stat, gid), srcs in line_book.items():
        g, ka = games.get(gid), srcs.get("kalshi")
        if not g or not ka or ka.get("line") is None:
            continue
        books = {b: srcs[b]["line"] for b in BOOKS if srcs.get(b) and srcs[b].get("line") is not None}
        if not books:
            continue
        k = float(ka["line"])
        diffs = [v - k for v in books.values()]
        if any(d == 0 for d in diffs) or len({d > 0 for d in diffs}) > 1:
            continue  # a book agrees with Kalshi, or the books point opposite ways
        gap = sum(abs(d) for d in diffs) / len(diffs)
        if gap < LAG_MIN:
            continue
        side = "OVER" if diffs[0] > 0 else "UNDER"
        n = int(round(k + 0.5))
        rung = next((r for r in ka.get("ladder") or [] if r.get("n") == n), None) or {}
        raw = rung.get("ask") if side == "OVER" else (1 - rung["bid"] if rung.get("bid") else None)
        cost = kalshi_client.with_fee(raw) if raw else None
        out.append({"date": g["date"], "game_id": gid, "tip_utc": g["tip_utc"].isoformat(), "player": player, "stat": stat,
                    "kalshi_line": k, "books": books, "gap": round(gap, 2), "side": side,
                    "bet": f"{'YES' if side == 'OVER' else 'NO'} {player} {n}+ {stat.upper()}",
                    "label": f"{player} {side} {k:g} {stat.upper()}", "mid": rung.get("mid"),
                    "ask": round(raw, 3) if raw else None, "cost": cost,
                    "spread": round(rung["ask"] - rung["bid"], 3) if rung.get("ask") and rung.get("bid") else None,
                    "key": predictions.key(player, stat, gid)})
    return sorted(out, key=lambda r: -r["gap"])


def record(lags: list[dict], now: datetime | None = None) -> int:
    """New lags are added; open ones keep their latest price until tip (the first price is kept too)."""
    now = now or datetime.now(timezone.utc)
    stamp = now.strftime("%Y-%m-%d %H:%M UTC")
    by_date, n = {}, 0
    for r in lags:
        if now >= datetime.fromisoformat(r["tip_utc"]):
            continue
        d = by_date.setdefault(r["date"], _load(r["date"]))
        k = f"{r['game_id']}|{r['player']}|{r['stat']}"
        cur = d.get(k)
        if not cur:
            d[k] = {**r, "first_seen": stamp, "last_seen": stamp, "first_cost": r["cost"], "status": "pending", "hit": None}
            n += 1
        elif cur["status"] == "pending" and cur["side"] == r["side"] and cur["kalshi_line"] == r["kalshi_line"]:
            cur.update({x: r[x] for x in ("books", "gap", "mid", "ask", "cost", "spread")}, last_seen=stamp)
    for date, d in by_date.items():
        _save(date, d)
    return n


def grade(now: datetime | None = None) -> int:
    """From the player's graded pick (same player-stat-game). No pick record at all (a player the site doesn't
    project, e.g. ruled out) -> void 36 hours after tip, so nothing stays pending forever."""
    now = now or datetime.now(timezone.utc)
    n = 0
    for date in all_dates():
        d = _load(date)
        ps = predictions.load_date(date)
        changed = False
        for r in d.values():
            if r["status"] != "pending":
                continue
            p = ps.get(r["key"])
            if not p and (now - datetime.fromisoformat(r["tip_utc"])).total_seconds() > 36 * 3600:
                r.update(status="void", hit=None, units=0.0, note="no pick record for this player")
                changed = True
                n += 1
                continue
            if not p or (p.get("status") != "void" and p.get("actual") is None):
                continue
            a = p.get("actual")
            if p.get("status") == "void" or a is None or a == r["kalshi_line"]:
                r.update(status="void", hit=None, units=0.0)
            else:
                hit = (a > r["kalshi_line"]) == (r["side"] == "OVER")
                r.update(status="graded", hit=hit, actual=a,
                         units=round((1 / r["cost"] - 1) if hit else -1.0, 4) if r.get("cost") else None)
            changed = True
            n += 1
        if changed:
            _save(date, d)
    return n


def lag_summary(today: str | None = None) -> dict:
    allr = [r for d in all_dates() for r in _load(d).values()]
    g = [r for r in allr if r["status"] == "graded"]
    priced = [r for r in g if r.get("cost")]
    h = sum(1 for r in g if r["hit"])
    hp = sum(1 for r in priced if r["hit"])
    avg_cost = sum(r["cost"] for r in priced) / len(priced) if priced else None
    low = proven.wilson_low(hp, len(priced)) if priced else 0.0
    earned = len(priced) >= LAG_EARN_N and avg_cost is not None and low > avg_cost
    return {"n": len(g), "hits": h, "rate": round(100 * h / len(g), 1) if g else None,
            "priced": len(priced), "priced_hits": hp, "avg_cost": round(100 * avg_cost, 1) if avg_cost else None,
            "units": round(sum(r["units"] for r in priced), 2), "low": round(100 * low, 1), "earned": earned,
            "earn_n": LAG_EARN_N, "pending": sum(1 for r in allr if r["status"] == "pending"),
            "today": sorted([r for r in allr if r["date"] == today and r["status"] == "pending"], key=lambda r: -r["gap"]),
            "recent": sorted(g, key=lambda r: (r["date"], r["player"]), reverse=True)[:60]}


def trust_table(preds: list[dict]) -> dict:
    """Every graded player pick by source, and Kalshi by price band: does the line deserve trust?"""
    def be(p):
        b = p.get("break_even")
        return b if b and 0.05 < b < 0.98 else 0.5238

    def row(name, rs):
        n = len(rs)
        h = sum(1 for p in rs if p["hit"])
        need = sum(be(p) for p in rs) / n
        u = sum((1 / be(p) - 1) if p["hit"] else -1 for p in rs)
        low = proven.wilson_low(h, n)
        v = "TRUSTED" if low > need else ("CLOSE" if h / n >= need - 0.02 else "NOT YET")
        return {"name": name, "n": n, "rate": round(100 * h / n, 1), "need": round(100 * need, 1),
                "units": round(u, 1), "roi": round(100 * u / n, 1), "low": round(100 * low, 1), "verdict": v}

    g = [p for p in preds if p.get("status") == "graded" and p.get("hit") is not None]
    label = {"kalshi": "Kalshi", "prizepicks": "PrizePicks", "fanduel": "FanDuel", "prizepicks+kalshi": "PrizePicks (Kalshi-priced)"}
    by_src, by_band = {}, {}
    for p in g:
        s = p.get("src_key") or p.get("line_source")
        by_src.setdefault(s, []).append(p)
        if s == "kalshi" and p.get("market_side") is not None:
            b = int(p["market_side"] * 20) * 5
            by_band.setdefault(b, []).append(p)
    return {"sources": [row(label.get(s, s), rs) for s, rs in sorted(by_src.items(), key=lambda x: -len(x[1])) if len(rs) >= 30],
            "kalshi_bands": [row(f"Kalshi {b}–{b + 5}%", rs) for b, rs in sorted(by_band.items()) if len(rs) >= 30],
            "n": len(g)}


def ladder_snapshot(ln: dict | None) -> list | None:
    """Kalshi ladder as [[rung, bid, ask], ...] for the pick record."""
    if not ln or not ln.get("ladder"):
        return None
    return [[r["n"], r.get("bid"), r.get("ask")] for r in ln["ladder"]]
