"""3x slip (update 100, Nov 23 2026): the single Kalshi combo most likely to cash while paying at least 3x.

The user's ask: "at least 3x my money: the best possible slip that can achieve that."
Leg pool, every build until the slip's first leg tips (Kalshi rung legs only, one leg per player, max two per game,
game not started, player not listed out/doubtful/questionable, no MINUTES DOWN flag, 5+ games this season, Kalshi ask 50-88%):
  - 2+ threes when the projection is 1.5-2.5 and he made 2+ in 7 of his last 10
    (Oct 31-Nov 19 replay: 2+ threes priced 61% hit 64%; projection 1.5-2.5 +7..13%/bet; 7+/10 form +7.7%/bet;
     1+ threes LOSE -4.6%/bet and are never used);
  - 10+ points when the projection is 2-4 above the rung (75% vs 70% priced, +6.9%/bet; below 2 -15%, above 4 Kalshi
    already prices it at ~80% and it lost);
  - 4+ rebounds when the projection is 2-3 above (+4%; under 2 above, rebounds lost 10-16%/bet: the trap);
  - 2+/4+ assists when the projection is 1-3 above.
Choice: of every combo of 2-6 legs from the 22 best-edge legs whose estimated Kalshi quote is 3x+ (product of asks x 0.85,
because Kalshi quotes combos itself below the product), the one with the highest chance of cashing, using each leg's
chance = half model, half Kalshi price (shrunk: the model alone over-rates its legs).
Replay (rules were found on the same Oct 31-Nov 19 legs, so this flatters them): 9 of 16 nights cashed, +11.3U at
0.85 x product; out of sample Nov 20-22: 2 of 3 cashed. Small samples: tracked here and graded every night before
anyone should trust it.
"""
from __future__ import annotations

import itertools
import json
import math
from collections import Counter
from datetime import datetime, timezone
from datetime import date as _date
from pathlib import Path

import tipsters as T

PATH = Path(__file__).resolve().parent.parent / "data" / "tipsters" / "triple.json"
TARGET = 3.0          # estimated Kalshi quote
HAIRCUT = 0.85        # Kalshi combo quote vs product of leg asks (short slips)
ASK_LO, ASK_HI = 0.50, 0.88
MAX_LEGS, MAX_CAND, PER_GAME = 6, 22, 2
WIN = {"pts": (10, 2.0, 4.0), "reb": (4, 2.0, 3.0)}
NAMES = {"pts": "points", "reb": "rebounds", "ast": "assists", "fg3m": "threes"}


def _load() -> dict:
    try:
        return json.loads(PATH.read_text())
    except Exception:  # noqa: BLE001
        return {}


def _bad_status(inj) -> set:
    return {T._norm(p.get("player")) for p in (inj or {}).get("players") or []
            if str(p.get("status") or "").lower() in ("out", "doubtful", "questionable")}


def fits(st: str, rung: int, proj: float, h10: float | None) -> str | None:
    """Which rule a rung leg fits (None = not in the pool)."""
    if st == "fg3m":
        return "2+ threes, projection 1.5-2.5, 7+/10" if rung == 2 and 1.5 <= proj < 2.5 and (h10 or 0) >= 0.7 else None
    if st == "ast":
        return "assists, projection 1-3 above" if rung in (2, 4) and 1.0 <= proj - rung < 3.0 else None
    if st in WIN:
        r, lo, hi = WIN[st]
        if rung == r and lo <= proj - rung < hi:
            return f"{NAMES[st]}, projection {lo:g}-{hi:g} above"
    return None


def candidates(date: str, box, now: datetime, inj=None) -> list[dict]:
    import model
    import siteladder as SL
    D = _date.fromisoformat(date)
    logs = SL._logs(box)
    bad = _bad_status(inj)
    try:
        rows = json.loads((T.PRED_DIR / f"{date}.json").read_text())
    except Exception:  # noqa: BLE001
        return []
    out = []
    for r in rows.values():
        st = r.get("stat")
        if st not in NAMES or not r.get("kladder") or r.get("projected") is None:
            continue
        if T._norm(r["player"]) in bad:
            continue
        try:
            tip = datetime.fromisoformat(str(r.get("tip_utc")).replace("Z", "+00:00"))
        except ValueError:
            continue
        if now >= tip:
            continue
        lg = [x for x in logs.get(T._norm(r["player"]), []) if x["date"] < D]
        if len(lg) < 5:
            continue
        proj = float(r["projected"])
        mu, var = r.get("blend_mean") or r.get("projected"), r.get("var")
        for rung, _b, ask in r["kladder"]:
            if not ask or not ASK_LO <= ask <= ASK_HI:
                continue
            h10 = sum(x[st] >= rung for x in lg[-10:]) / len(lg[-10:])
            why = fits(st, rung, proj, h10)
            if not why:
                continue
            mp = model.prob_over(st, mu, var, rung - 0.5) if mu is not None and var else None
            if mp is None:
                continue
            md = T.minutes_flag(r)
            if md:          # projected minutes well under his recent minutes: skip, a short night sinks the slip
                continue
            cost = T._kcost(ask)
            out.append({"player": r["player"], "team": r.get("team"), "opp": r.get("opp"), "stat": st, "rung": rung,
                        "line": rung - 0.5, "side": "over", "ask": round(ask, 2), "p": round(cost, 4),
                        "model_p": round(mp, 3), "est": round(0.5 * mp + 0.5 * cost, 4), "proj": round(proj, 1),
                        "h10": round(h10, 2), "why": why, "game_id": r.get("game_id"), "tip": r.get("tip_utc"),
                        "min_down": None})
    return out


def choose(legs: list[dict], target: float = TARGET) -> dict | None:
    legs = sorted(legs, key=lambda l: -(l["est"] - l["p"]))[:MAX_CAND]
    best = None
    for k in range(2, MAX_LEGS + 1):
        for c in itertools.combinations(legs, k):
            if len({l["player"] for l in c}) < k or max(Counter(l["game_id"] for l in c).values()) > PER_GAME:
                continue
            payk = 1 / math.prod(l["ask"] for l in c)
            if payk * HAIRCUT < target:
                continue
            ch = math.prod(l["est"] for l in c)
            if best is None or ch > best["chance"]:
                best = {"legs": sorted(c, key=lambda l: l["tip"] or ""), "chance": round(ch, 4),
                        "pays_k": round(payk, 2), "quote_est": round(payk * HAIRCUT, 2)}
        if best and k >= 4:   # more legs only lower the chance once a 4-leg slip clears the target
            break
    return best


def sync(box, sl: dict, now: datetime | None = None, inj=None) -> None:
    now = now or datetime.now(timezone.utc)
    date = (sl or {}).get("date")
    if not date:
        return
    st = _load()
    cur = st.get(date)
    if cur and cur.get("locked"):
        return
    if cur and cur.get("legs"):
        tips = [datetime.fromisoformat(str(l["tip"]).replace("Z", "+00:00")) for l in cur["legs"] if l.get("tip")]
        if tips and now >= min(tips):
            cur["locked"] = True
            PATH.write_text(json.dumps(st, indent=1, sort_keys=True))
            return
    pick = choose(candidates(date, box, now, inj))
    if pick:
        st[date] = {**pick, "built": now.strftime("%Y-%m-%d %H:%M UTC"), "locked": False}
    else:
        st.pop(date, None)
    PATH.parent.mkdir(parents=True, exist_ok=True)
    PATH.write_text(json.dumps(st, indent=1, sort_keys=True))


def summary(box, today: str | None) -> dict:
    """Tonight's slip and the record of locked nights (1U at the estimated Kalshi quote)."""
    ply, team = T._box_index(box)
    nights, n, w, u, ln, lw = [], 0, 0, 0.0, 0, 0
    for d, s in sorted(_load().items(), reverse=True):
        legs, res = [], []
        for l in s["legs"]:
            g, x, rr = T.grade_leg(l, d, ply, team)
            legs.append({**l, "res": g, "actual": x, "cut": T.cut_short(d, l, rr) if g in ("hit", "miss") else None})
            res.append(g)
        status = "open" if any(r is None for r in res) else "graded"
        hit = status == "graded" and "miss" not in res and "hit" in res
        nights.append({"date": d, **s, "legs": legs, "status": status, "hit": hit})
        if status == "graded" and s.get("locked"):
            n += 1
            w += hit
            u += (s["quote_est"] - 1) if hit else -1
            ln += sum(r in ("hit", "miss") for r in res)
            lw += sum(r == "hit" for r in res)
    return {"tonight": next((x for x in nights if x["date"] == today), None),
            "last": next((x for x in nights if x["date"] != today and x["status"] == "graded"), None),
            "rec": {"n": n, "w": w, "u": round(u, 2), "legs_n": ln, "legs_w": lw}, "target": TARGET}
