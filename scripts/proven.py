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
        r = out.setdefault(k, {"h": 0, "n": 0, "b": 0.0})
        r["n"] += 1
        r["h"] += 1 if p["hit"] else 0
        be = p.get("break_even")
        r["b"] += be if be and 0.05 < be < 0.98 else 0.5238
    return out


# Earned stakes (update 51): a regular pick only carries a stake once its group (source + 5-point chance band)
# has EARN_N+ graded picks hitting at least EARN_MARGIN above what their prices needed. Walk-forward backtest
# Oct 20-30 (each day judged only on earlier days): every recommended stake -14.66U on 172U (-8.5%); earned
# only -0.43U on 25U (-1.7%). Groups earn (and lose) it automatically as results come in.
EARN_N = 30
EARN_MARGIN = 0.02


def earned(rec: dict | None) -> bool:
    if not rec or rec["n"] < EARN_N:
        return False
    need = rec.get("b", 0.5238 * rec["n"]) / rec["n"]
    return rec["h"] / rec["n"] >= need + EARN_MARGIN


# Best Value ranking (update 70): how often picks like this have REALLY hit (group record, lightly smoothed toward the
# stated chance) minus what this pick's price needs. Walk-forward test Oct 26 - Nov 5 (each day judged only on
# earlier days), 243 picks: the old ranking (our chance minus the price) -23.2U, 49.8%; this one +1.2U, better on
# 7 of 10 days, and every setting tried beat the old one by 19-24U. Listing only gaps above 0: same units on 170 picks,
# better on 9 of 10 days. Groups need EARN_N graded picks to rank.
VALUE_SHRINK = 5


def real_gap(p: dict, rec: dict | None) -> float | None:
    be = p.get("break_even")
    if not rec or rec["n"] < EARN_N or be is None or p.get("chance") is None:
        return None
    return (rec["h"] + VALUE_SHRINK * p["chance"]) / (rec["n"] + VALUE_SHRINK) - be


def earn_note(rec: dict | None) -> str:
    if not rec or not rec["n"]:
        return "no stake yet: no graded picks like this"
    need = rec.get("b", 0.5238 * rec["n"]) / rec["n"]
    return (f"no stake yet: picks like this are {rec['h']}/{rec['n']} ({100 * rec['h'] / rec['n']:.0f}%); a stake needs "
            f"{EARN_N}+ graded at {100 * (need + EARN_MARGIN):.0f}%+ (2 points over the {100 * need:.0f}% their prices need)")


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
