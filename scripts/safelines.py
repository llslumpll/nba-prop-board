"""Safe lines (Nov 19 2026): @PrizePicksTacos' daily "Top NBA plays" list, graded, with what each line costs you.

His list = PrizePicks goblin lines that the books price at about -600 to -1100 (average book odds, as he posts them).
The user follows him to find safe lines. What the site adds:
  - his real record by kind of line (points, rebounds, PA, PRA, ...) and by how short the posted odds were;
  - tonight: the best price for each line right now (Kalshi at the same number, Polymarket US, FanDuel at the same
    line, via tipsters.your_price) next to how often that kind of line from his list has really hit;
  - a verdict per line: GOOD PRICE (costs at least 1 point less than its kind's real hit rate), FAIR, TOO PRICEY,
    SKIP (kinds that hit under 80%: his PA and PRA lines), or NO PRICE (no matching number on Kalshi/Polymarket/FanDuel);
  - a "your price" record: the first price the site saw before tip, graded like the tail test.
Lists are logged by Claude into data/tipsters/safelines.json. Tracking only, no stakes.
"""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import tipsters as T

FILE = Path(__file__).resolve().parent.parent / "data" / "tipsters" / "safelines.json"
STATE = Path(__file__).resolve().parent.parent / "data" / "tipsters" / "safelines_state.json"
SKIP_BELOW = 0.80     # kinds of lines that hit less than this (shrunk) are tagged SKIP
SHRINK = 30           # pseudo-picks pulling small kinds toward his overall rate
MARGIN = 0.01         # GOOD PRICE / TOO PRICEY band around the kind's hit rate
KIND = {"pts": "Points", "reb": "Rebounds", "ast": "Assists", "fg3m": "Threes", "pr": "Pts + Reb",
        "pa": "Pts + Ast", "ra": "Reb + Ast", "pra": "PRA"}


def _imp(o: int) -> float:
    return -o / (-o + 100) if o < 0 else 100 / (o + 100)


def _bucket(o: int) -> str:
    return "-900 or shorter" if o <= -900 else ("-750 to -875" if o <= -750 else "-725 or longer")


def _rec(rs: list) -> dict:
    n = len(rs)
    w = sum(r["res"] == "hit" for r in rs)
    return {"n": n, "w": w, "l": n - w, "rate": round(100 * w / n, 1) if n else None,
            "imp": round(100 * sum(_imp(r["odds"]) for r in rs) / n, 1) if n else None}


def summary(box, sl: dict | None = None, now: datetime | None = None, today: str | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    data = T._load(FILE, {"days": {}})
    state = T._load(STATE, {})
    ply, _team = T._box_index(box)
    today = today or (sl or {}).get("date")
    graded, days = [], []
    for d in sorted(data.get("days", {})):
        rows = []
        for i, p in enumerate(data["days"][d]):
            leg = {"player": p["player"], "stat": p["stat"], "line": p["line"], "side": "over"}
            res, x, r = T.grade_leg(leg, d, ply)
            key = f"{d}|{i}"
            row = {**p, "key": key, "res": res, "actual": x, "team": r["team"] if r is not None else None,
                   "your": state.get(key)}
            rows.append(row)
            if res in ("hit", "miss"):
                graded.append({**row, "date": d})
        if any(r["res"] in ("hit", "miss") for r in rows):
            for r in rows:   # that night's games are in and he has no box-score row: he didn't play
                if r["res"] is None:
                    r["res"] = "void"
            days.append({"date": d, **_rec([r for r in rows if r["res"] in ("hit", "miss")]), "rows": rows})
    overall = _rec(graded)
    base = (overall["w"] / overall["n"]) if overall["n"] else 0.85
    kinds = {}
    for k in KIND:
        rs = [r for r in graded if r["stat"] == k]
        rec = _rec(rs)
        rec["fair"] = round((rec["w"] + base * SHRINK) / (rec["n"] + SHRINK), 4)
        rec["name"] = KIND[k]
        rec["skip"] = rec["fair"] < SKIP_BELOW
        kinds[k] = rec
    by_odds = [{"name": b, **_rec([r for r in graded if _bucket(r["odds"]) == b])}
               for b in ("-725 or longer", "-750 to -875", "-900 or shorter")]

    # tonight: price every line on today's list, record the first price seen before tip
    tonight = []
    if today and today in data.get("days", {}):
        preds = T._pred_rows(today)
        changed = False
        for i, p in enumerate(data["days"][today]):
            key = f"{today}|{i}"
            prow = preds.get((T._norm(p["player"]), p["stat"]))
            leg = {"player": p["player"], "stat": p["stat"], "line": p["line"], "side": "over"}
            your = T.your_price(leg, prow) if prow else None
            tip = None
            if prow and prow.get("tip_utc"):
                try:
                    tip = datetime.fromisoformat(str(prow["tip_utc"]).replace("Z", "+00:00"))
                except ValueError:
                    tip = None
            if your and key not in state and (tip is None or now < tip):
                state[key] = {"p": your["p"], "src": your["src"], "at": now.strftime("%Y-%m-%d %H:%M UTC")}
                changed = True
            k = kinds.get(p["stat"]) or {"fair": base, "skip": False, "name": p["stat"]}
            cost = your["p"] if your else None
            if k["skip"]:
                tag = "skip"
            elif cost is None:
                tag = "noprice"
            elif cost <= k["fair"] - MARGIN:
                tag = "good"
            elif cost <= k["fair"] + MARGIN:
                tag = "fair"
            else:
                tag = "pricey"
            done = next((r for dd in days if dd["date"] == today for r in dd["rows"] if r["key"] == key), None)
            tonight.append({**p, "key": key, "your": your, "your_odds": T.p_to_am(cost) if cost else None,
                            "cost": cost, "fair": k["fair"], "kind": k["name"], "tag": tag,
                            "team": prow.get("team") if prow else None, "game_id": prow.get("game_id") if prow else None,
                            "res": done["res"] if done else None, "actual": done["actual"] if done else None})
        if changed and FILE.parent.exists():
            STATE.write_text(json.dumps(state, indent=1, sort_keys=True))
        order = {"good": 0, "fair": 1, "pricey": 2, "noprice": 3, "skip": 4}
        tonight.sort(key=lambda r: (order[r["tag"]], -(r["fair"] - (r["cost"] or 1))))

    # "your price" record: picks graded at the first price the site saw before tip (Nov 19 on)
    yp = [r for r in graded if r.get("your")]
    yu = sum((1 / r["your"]["p"] - 1) if r["res"] == "hit" else -1 for r in yp)
    your_rec = {"n": len(yp), "w": sum(r["res"] == "hit" for r in yp), "u": round(yu, 2),
                "cost": round(100 * sum(r["your"]["p"] for r in yp) / len(yp), 1) if yp else None}
    your_rec["l"] = your_rec["n"] - your_rec["w"]
    return {"source": data.get("source"), "what": data.get("what"), "overall": overall, "kinds": kinds,
            "kind_order": sorted(kinds, key=lambda k: -kinds[k]["n"]), "by_odds": by_odds,
            "days": list(reversed(days)), "tonight": tonight, "today": today,
            "listed_today": bool(today and today in data.get("days", {})), "your_rec": your_rec,
            "skip_below": int(SKIP_BELOW * 100), "margin": int(MARGIN * 100), "lab": T.LAB, "p_to_am": T.p_to_am}
