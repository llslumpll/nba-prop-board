"""Favorites tab (update 80): the user's moneyline rule, tracked.

Rule (the user's own): bet a favorite only at 75%+ (prefers 80%+), ideally when its recent form (last 5, up to 10
games) is better than its price. Every favorite at 75%+ is listed and graded; "form beats price" is a tag, not a
filter, so we can see whether it helps.

Season record: data/team_predictions/*.json moneyline rows already store the closing no-vig chance (close_mkt) and
the final score, so the record rebuilds itself every build from opening night on. Price paid is estimated as the
closing chance + 2 points (typical vig/fee), since the cheapest real price wasn't stored for past games.
Tonight: fair chance from FanDuel (no-vig) or Kalshi, plus what each place charges for the favorite:
FanDuel, Kalshi (ask + fee) and Polymarket US (ask + its fee). Tracking only, no stakes.
"""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import date as _date
from pathlib import Path

TP_DIR = Path(__file__).resolve().parent.parent / "data" / "team_predictions"
MIN_P = 0.75
EST_VIG = 0.02
KFEE = 0.07


def _am(p: float | None) -> str | None:
    if not p or p <= 0 or p >= 1:
        return None
    return f"-{round(100 * p / (1 - p))}" if p >= 0.5 else f"+{round(100 * (1 - p) / p)}"


def _fd_cost(price) -> float | None:
    if price is None:
        return None
    return (-price) / (-price + 100) if price < 0 else 100 / (price + 100)


def _form_index(box):
    """team -> [(date_iso, won)] sorted, current season."""
    import polars as pl
    hist = defaultdict(list)
    if box is None or not box.height:
        return hist
    cur = (box.filter(pl.col("date") >= _date(2026, 10, 1)).select("date", "team", "team_score", "opp_score")
           .unique().drop_nulls().sort("date"))
    for d, t, ts, os_ in cur.iter_rows():
        hist[t].append((d.isoformat(), ts > os_))
    return hist


def _form(hist, team, before, n):
    g = [w for d, w in hist.get(team, []) if d < before][-n:]
    return g


def _tag(hist, team, before, p):
    f10, f5 = _form(hist, team, before, 10), _form(hist, team, before, 5)
    beats = len(f10) >= 5 and sum(f10) / len(f10) > p
    return {"l5": f"{sum(f5)}-{len(f5) - sum(f5)}" if f5 else "-", "l10": f"{sum(f10)}-{len(f10) - sum(f10)}" if f10 else "-",
            "n": len(f10), "beats": beats, "l10_pct": round(100 * sum(f10) / len(f10)) if f10 else None}


def history(hist) -> list[dict]:
    rows = []
    for f in sorted(TP_DIR.glob("*.json")):
        try:
            d = json.loads(f.read_text())
        except Exception:  # noqa: BLE001
            continue
        for r in d.values():
            if r.get("market") != "moneyline" or not r.get("scores") or r.get("close_mkt") is None:
                continue
            pm, pick = r["close_mkt"], r["pick"]
            other = r["home"] if pick == r["away"] else r["away"]
            fav, fp = (pick, pm) if pm >= 0.5 else (other, 1 - pm)
            if fp < MIN_P:
                continue
            hs, as_ = r["scores"]
            winner = r["home"] if hs > as_ else r["away"]
            dog = r["away"] if fav == r["home"] else r["home"]
            cost = min(0.99, fp + EST_VIG)
            won = winner == fav
            rows.append({"date": r["date"], "fav": fav, "dog": dog, "home": r["home"], "away": r["away"], "p": round(fp, 3),
                         "cost": round(cost, 3), "won": won, "final": r.get("final"),
                         "units": round((1 / cost - 1) if won else -1.0, 3), **_tag(hist, fav, r["date"], fp)})
    rows.sort(key=lambda x: (x["date"], x["fav"]))
    return rows


def _rec(rs):
    n = len(rs)
    if not n:
        return {"n": 0, "w": 0, "l": 0, "u": 0.0, "rate": None, "imp": None}
    w = sum(r["won"] for r in rs)
    return {"n": n, "w": w, "l": n - w, "u": round(sum(r["units"] for r in rs), 2), "rate": round(100 * w / n, 1),
            "imp": round(100 * sum(r["p"] for r in rs) / n, 1)}


def tonight(sl, hist) -> list[dict]:
    import polymarket_client
    out = []
    for g in (sl or {}).get("games") or []:
        if g.get("started") or g.get("completed"):
            continue
        fd = (g.get("fanduel") or {}).get("moneyline") or {}
        km = g.get("kalshi_ml") or {}
        hp = fd.get("home_prob") if fd.get("home_prob") is not None else km.get("home_prob")
        if hp is None:
            continue
        hp /= 100
        home_fav = hp >= 0.5
        fav, dog = (g["home"], g["away"]) if home_fav else (g["away"], g["home"])
        fp = hp if home_fav else 1 - hp
        if fp < MIN_P:
            continue
        side = "home" if home_fav else "away"
        prices = []
        c = _fd_cost(fd.get(f"{side}_price"))
        if c:
            prices.append({"book": "FanDuel", "p": round(c, 4)})
        ka = km.get(f"{side}_ask")
        if ka:
            ka = ka / 100 if ka > 1 else ka
            prices.append({"book": "Kalshi", "p": round(ka + KFEE * ka * (1 - ka), 4)})
        pc = polymarket_client.ml_cost(fav, g["game_id"])
        if pc:
            prices.append({"book": "Polymarket", "p": pc})
        for x in prices:
            x["odds"] = _am(x["p"])
        best = min(prices, key=lambda x: x["p"]) if prices else None
        out.append({"game_id": g["game_id"], "fav": fav, "dog": dog, "home": g["home"], "away": g["away"], "tip": g.get("tip_et"),
                    "p": round(fp, 3), "fair": _am(fp), "prices": prices, "best": best, "tier80": fp >= 0.8,
                    **_tag(hist, fav, sl["date"], fp)})
    out.sort(key=lambda x: -x["p"])
    return out


def summary(box, sl) -> dict:
    hist = _form_index(box)
    rows = history(hist)
    ok = [r for r in rows if r["n"] >= 5]
    by_day = defaultdict(list)
    for r in rows:
        by_day[r["date"]].append(r)
    days = [{"date": d, "rows": rs, **_rec(rs)} for d, rs in sorted(by_day.items(), reverse=True)]
    return {"tonight": tonight(sl, hist), "days": days, "min_p": MIN_P, "est_vig": EST_VIG,
            "rec": {"all": _rec(rows), "p80": _rec([r for r in rows if r["p"] >= 0.8]),
                    "p75": _rec([r for r in rows if r["p"] < 0.8]),
                    "beats": _rec([r for r in ok if r["beats"]]), "nobeat": _rec([r for r in ok if not r["beats"]])},
            "upsets": [r for r in rows if not r["won"]][-10:]}
