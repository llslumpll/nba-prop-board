"""
Proven picks: "picks like this hit 18 of 20".

Every graded pick goes into a group: where its line came from (Kalshi,
PrizePicks, PrizePicks priced from Kalshi, FanDuel; for game picks the market)
and its stated chance in 5-point bands (70-75%, 75-80%, ...). Each build the
group's real record is attached to every open pick in it, so the card shows
how often picks exactly like it have cashed, not just the chance the model
states.

Proven units: a group earns big stakes only by cashing.
  2U  PROVEN    20+ graded, 80%+ hit, and even the cautious end of its hit
                rate (95% Wilson lower bound) beats THIS pick's break-even
  3U  PROVEN+   30+ graded, 85%+ hit, lower bound 3+ points over break-even
A proven group lifts a pick's stake to that level (never lowers it); a SKIP
stays a SKIP. Groups that stop cashing lose the status on the next build.
"""

from __future__ import annotations

import math

BAND = 5          # percentage points per chance band
MIN_N = 20
PLUS_N = 30


def band(chance: float) -> int:
    return int(min(95, max(0, math.floor(chance * 100 / BAND) * BAND)))


def src_key(p: dict) -> str:
    """Same grouping the patterns engine uses for sources."""
    if p.get("market"):
        return p["market"]
    if p.get("anchor") == "kalshi":
        return "prizepicks+kalshi"
    return p.get("line_source") or p.get("source") or "?"


def key(p: dict, chance: float | None = None) -> str:
    return f"{src_key(p)}|{band(p['chance'] if chance is None else chance)}"


def compute(preds: list[dict], tpreds: list[dict]) -> dict:
    out = {}
    for p in preds + tpreds:
        if p.get("status") != "graded" or p.get("hit") is None or p.get("chance") is None:
            continue
        k = key(p)
        r = out.setdefault(k, {"h": 0, "n": 0})
        r["n"] += 1
        r["h"] += 1 if p["hit"] else 0
    return out


def wilson_low(h: int, n: int, z: float = 1.96) -> float:
    if not n:
        return 0.0
    p = h / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    m = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (c - m) / d


def units(rec: dict | None, be: float | None) -> float | None:
    """Stake a proven group earns for a pick at break-even `be` (None = not proven)."""
    if not rec or rec["n"] < MIN_N:
        return None
    be = be if be is not None else 0.5238
    rate, lo = rec["h"] / rec["n"], wilson_low(rec["h"], rec["n"])
    if rec["n"] >= PLUS_N and rate >= 0.85 and lo >= be + 0.03:
        return 3.0
    if rate >= 0.80 and lo >= be:
        return 2.0
    return None


def label(rec: dict | None) -> str | None:
    return f"{rec['h']}/{rec['n']}" if rec and rec["n"] else None
