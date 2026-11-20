"""Site ladders (update 89, Nov 20 2026): the site builds its own Sam-style lotto ladders and races his.

Sam's pattern (21 slips, 62 legs checked Nov 20): three role players at one standard Kalshi rung (10+ points,
4+ assists, 4+ rebounds, 2+ threes), priced about 55-80%; his legs hit 74% against 67% prices.
The site's version, every night, until the first leg tips:
  - candidates: every player with a Kalshi ladder at that rung costing 55-82% (fee in), 5+ games this season,
    game not started;
  - score = average of the model's chance and his last-10 hit rate at that rung, minus the price
    (the best of several selectors in the Oct 31-Nov 19 replay; none beat the price on the legs yet:
    legs 65% vs 67% price, so this is a race to watch, not a recommendation);
  - per stat, the three best scores from different games make a ladder; the two stats with the best average
    score are posted. Each ladder also has step 2 and step 3 (the same three players one and two rungs up:
    +5 points, +2 assists/rebounds, +1 three) when Kalshi lists them.
Locked at the first tip, graded the next morning, compared with Sam's safest slip. Tracking only, no stakes.
"""
from __future__ import annotations

import json
import re
import statistics
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import tipsters as T

PATH = Path(__file__).resolve().parent.parent / "data" / "tipsters" / "siteladder.json"
RUNG = {"pts": 10, "ast": 4, "reb": 4, "fg3m": 2}
STEP = {"pts": 5, "ast": 2, "reb": 2, "fg3m": 1}
LO, HI = 0.55, 0.82
MIN_GAMES = 5
LADDERS_PER_NIGHT = 2
NAMES = {"pts": "Points", "ast": "Assists", "reb": "Rebounds", "fg3m": "Threes"}


def _logs(box):
    import polars as pl
    import dataio
    import model
    cur = model._with_combos(box.filter(pl.col("played") & (pl.col("season") == dataio.CURRENT_SEASON))).sort("date")
    out = defaultdict(list)
    for r in cur.select("player", "date", "pts", "reb", "ast", "fg3m").iter_rows(named=True):
        out[T._norm(r["player"])].append(r)
    return out


def _load() -> dict:
    try:
        return json.loads(PATH.read_text())
    except Exception:  # noqa: BLE001
        return {}


def build_night(date: str, box, now: datetime) -> list[dict]:
    import model
    from datetime import date as _d
    D = _d.fromisoformat(date)
    logs = _logs(box)
    try:
        rows = json.loads((T.PRED_DIR / f"{date}.json").read_text())
    except Exception:  # noqa: BLE001
        return []
    by_stat = defaultdict(list)
    for r in rows.values():
        st = r.get("stat")
        if st not in RUNG or not r.get("kladder"):
            continue
        try:
            tip = datetime.fromisoformat(str(r.get("tip_utc")).replace("Z", "+00:00"))
        except ValueError:
            continue
        if now >= tip:
            continue
        lad = {a: c for a, _b, c in r["kladder"] if c}
        rung = RUNG[st]
        if rung not in lad:
            continue
        cost = T._kcost(lad[rung])
        if not LO <= cost <= HI:
            continue
        lg = [x for x in logs.get(T._norm(r["player"]), []) if x["date"] < D]
        if len(lg) < MIN_GAMES:
            continue
        mu, var = r.get("blend_mean") or r.get("projected"), r.get("var")
        p = model.prob_over(st, mu, var, rung - 0.5) if mu is not None and var else None
        if p is None:
            continue
        h10 = sum(x[st] >= rung for x in lg[-10:]) / len(lg[-10:])
        by_stat[st].append({"player": r["player"], "stat": st, "rung": rung, "line": rung - 0.5, "side": "over",
                            "p": round(cost, 4), "model_p": round(p, 3), "h10": round(h10, 2),
                            "score": round((p + h10) / 2 - cost, 4), "game_id": r.get("game_id"), "team": r.get("team"),
                            "tip": r.get("tip_utc"), "lad": lad, "min_down": T.minutes_flag(r),
                            "_mu": mu, "_var": var, "_last10": [x[st] for x in lg[-10:]]})
    ladders = []
    for st, cs in by_stat.items():
        pick, games = [], set()
        for c in sorted(cs, key=lambda c: -c["score"]):
            if c["game_id"] in games:
                continue
            pick.append(c)
            games.add(c["game_id"])
            if len(pick) == 3:
                break
        if len(pick) < 3:
            continue
        tiers = []
        for k in range(3):
            legs = []
            for c in pick:
                rung = c["rung"] + k * STEP[st]
                ask = c["lad"].get(rung)
                if not ask:
                    break
                mp = model.prob_over(st, c["_mu"], c["_var"], rung - 0.5)
                legs.append({**{x: c[x] for x in ("player", "stat", "game_id", "team", "min_down")},
                             "model_p": round(mp, 3) if mp is not None else None,
                             "h10": round(sum(v >= rung for v in c["_last10"]) / len(c["_last10"]), 2),
                             "rung": rung, "line": rung - 0.5, "side": "over", "p": round(T._kcost(ask), 4), "ask": round(ask, 2)})
            if len(legs) < 3:
                break
            pay = 1.0
            for l in legs:
                pay /= l["p"]
            payk = 1.0
            for l_ in legs:
                payk /= l_["ask"]
            tiers.append({"step": k + 1, "legs": legs, "pays": round(pay, 2), "pays_k": round(payk, 2)})
        ladders.append({"stat": st, "name": NAMES[st], "score": round(statistics.mean(c["score"] for c in pick), 4),
                        "tiers": tiers})
    ladders.sort(key=lambda l: -l["score"])
    return ladders[:LADDERS_PER_NIGHT]


def sync(box, sl: dict, now: datetime | None = None) -> None:
    now = now or datetime.now(timezone.utc)
    date = (sl or {}).get("date")
    if not date:
        return
    st = _load()
    cur = st.get(date)
    if cur and cur.get("locked"):
        return
    if cur and cur.get("ladders"):
        tips = [datetime.fromisoformat(str(l.get("tip") or "").replace("Z", "+00:00")) for lad in cur["ladders"]
                for t in lad["tiers"][:1] for l in t["legs"] if l.get("tip")]
        if tips and now >= min(tips):
            cur["locked"] = True
            PATH.write_text(json.dumps(st, indent=1, sort_keys=True))
            return
    ladders = build_night(date, box, now)
    for lad in ladders:   # keep each leg's tip for locking; drop the price table
        for t in lad["tiers"]:
            for l in t["legs"]:
                l.pop("lad", None)
    rows = {}
    try:
        rows = json.loads((T.PRED_DIR / f"{date}.json").read_text())
    except Exception:  # noqa: BLE001
        pass
    tipmap = {r.get("game_id"): r.get("tip_utc") for r in rows.values()}
    for lad in ladders:
        for t in lad["tiers"]:
            for l in t["legs"]:
                l["tip"] = tipmap.get(l["game_id"])
    if ladders:
        st[date] = {"ladders": ladders, "built": now.strftime("%Y-%m-%d %H:%M UTC"), "locked": False}
    else:
        st.pop(date, None)
    PATH.parent.mkdir(parents=True, exist_ok=True)
    PATH.write_text(json.dumps(st, indent=1, sort_keys=True))


def summary(box, today: str | None, sam: dict | None = None) -> dict:
    ply, team = T._box_index(box)
    st = _load()
    nights, rec = [], defaultdict(lambda: {"n": 0, "w": 0, "u": 0.0})
    legs_rec = {"n": 0, "w": 0, "p": 0.0}
    for d, night in sorted(st.items(), reverse=True):
        lads = []
        for lad in night.get("ladders") or []:
            tiers = []
            for t in lad["tiers"]:
                legs, res = [], []
                for l in t["legs"]:
                    g, x, _ = T.grade_leg(l, d, ply, team)
                    legs.append({**l, "res": g, "actual": x})
                    res.append(g)
                status = "open" if any(r is None for r in res) else "graded"
                hit = status == "graded" and "miss" not in res and "hit" in res
                tiers.append({**t, "legs": legs, "status": status, "hit": hit})
                if status == "graded" and night.get("locked"):
                    r = rec[t["step"]]
                    r["n"] += 1
                    r["w"] += hit
                    r["u"] += (t["pays"] - 1) if hit else -1
                    if t["step"] == 1:
                        for l in legs:
                            if l["res"] in ("hit", "miss"):
                                legs_rec["n"] += 1
                                legs_rec["w"] += l["res"] == "hit"
                                legs_rec["p"] += l["p"]
            lads.append({**lad, "tiers": tiers})
        nights.append({"date": d, "ladders": lads, "locked": night.get("locked")})
    for r in rec.values():
        r["u"] = round(r["u"], 2)
    legs_rec["price"] = round(100 * legs_rec["p"] / legs_rec["n"], 1) if legs_rec["n"] else None
    if sam is None:
        sam = {"n": 0, "w": 0, "p": 0.0}
        for pk in T._load(T.PICKS, {}).get("picks", []):
            if pk["tipster"] != "sam":
                continue
            rows = T._pred_rows(pk["date"])
            for leg in T.grade_pick(pk, ply, team).get("legs") or []:
                if leg.get("res") not in ("hit", "miss"):
                    continue
                pr = rows.get((T._norm(leg["player"]), leg["stat"]))
                ask = {a: c for a, _b, c in (pr or {}).get("kladder") or [] if c}.get(int(float(leg["line"]) + 0.5))
                sam["n"] += 1
                sam["w"] += leg["res"] == "hit"
                if ask:
                    sam["p"] += T._kcost(ask)
                    sam["np"] = sam.get("np", 0) + 1
        sam["price"] = round(100 * sam["p"] / sam["np"], 1) if sam.get("np") else None
    return {"tonight": next((n for n in nights if n["date"] == today), None), "nights": nights[:7],
            "rec": {k: rec[k] for k in sorted(rec)}, "legs": legs_rec, "sam": sam}


# ---- update 96 (Nov 20 2026): Sam + model ---------------------------------------------------------------------
# Review of Sam's 62 graded legs: when our projection clears his rung by a cushion (points +2, others +1) his legs went
# 18-1 against a 73% Kalshi price; the rest 28-15 against 63%. Across the whole slate (4,998 rung legs) the same rule
# does NOT beat the price, so it is used only as a filter on the legs Sam picks. Step 1 only: his players one rung up hit
# 38% vs 37% priced, two rungs up 6% vs 17%.
BEST_PATH = PATH.parent / "sambest.json"
SAM_CUSH = {"pts": 2.0, "reb": 1.0, "ast": 1.0, "fg3m": 1.0}


def sam_check(leg: dict, prow: dict | None) -> dict | None:
    """Does our projection clear Sam's rung by the cushion? Returns {"ok", "proj", "rung", "ask", "p", "pmin"}."""
    if not prow or prow.get("projected") is None or leg.get("side", "over") != "over":
        return None
    st, rung = leg["stat"], int(float(leg["line"]) + 0.5)
    proj = float(prow["projected"])
    lad = {a: c for a, _b, c in prow.get("kladder") or [] if c}
    ask = lad.get(rung)
    return {"ok": proj - rung >= SAM_CUSH.get(st, 1.0), "proj": round(proj, 1), "rung": rung,
            "ask": round(ask, 2) if ask else None, "p": round(T._kcost(ask), 4) if ask else None,
            "pmin": round(prow["proj_min"]) if prow.get("proj_min") else None, "game_id": prow.get("game_id"),
            "tip": prow.get("tip_utc")}


def _load_best() -> dict:
    try:
        return json.loads(BEST_PATH.read_text())
    except Exception:  # noqa: BLE001
        return {}


def sync_best(sl: dict, now: datetime | None = None) -> None:
    """Tonight's 'Sam's best legs' slip: every leg Sam posted today that passes sam_check, at Kalshi asks. Rebuilt every
    build until its first leg tips, then locked."""
    now = now or datetime.now(timezone.utc)
    date = (sl or {}).get("date")
    if not date:
        return
    st = _load_best()
    cur = st.get(date)
    if cur and cur.get("locked"):
        return
    if cur and cur.get("legs"):
        tips = [datetime.fromisoformat(str(l["tip"]).replace("Z", "+00:00")) for l in cur["legs"] if l.get("tip")]
        if tips and now >= min(tips):
            cur["locked"] = True
            BEST_PATH.write_text(json.dumps(st, indent=1, sort_keys=True))
            return
    rows = T._pred_rows(date)
    legs, seen = [], set()
    for pk in T._load(T.PICKS, {}).get("picks", []):
        if pk["tipster"] != "sam" or pk["date"] != date:
            continue
        for leg in pk.get("legs") or []:
            key = (T._norm(leg["player"]), leg["stat"], leg["line"])
            if key in seen:
                continue
            seen.add(key)
            c = sam_check(leg, rows.get((T._norm(leg["player"]), leg["stat"])))
            if c and c["ok"] and c["ask"]:
                try:
                    started = now >= datetime.fromisoformat(str(c["tip"]).replace("Z", "+00:00"))
                except ValueError:
                    started = False
                if not started:
                    legs.append({"player": leg["player"], "stat": leg["stat"], "line": leg["line"], "side": "over",
                                 "rung": c["rung"], "ask": c["ask"], "p": c["p"], "proj": c["proj"], "pmin": c["pmin"],
                                 "game_id": c["game_id"], "tip": c["tip"]})
    if len(legs) >= 2:
        payk = 1.0
        for l in legs:
            payk /= l["ask"]
        st[date] = {"legs": legs, "pays_k": round(payk, 2), "built": now.strftime("%Y-%m-%d %H:%M UTC"), "locked": False}
    else:
        st.pop(date, None)
    BEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    BEST_PATH.write_text(json.dumps(st, indent=1, sort_keys=True))


def best_summary(box, today: str | None) -> dict:
    """Tonight's slip, its record (locked nights, 1U at Kalshi asks), and the season record of every Sam leg that
    passes / fails the check (from saved projections; tracked point in time from Nov 20)."""
    ply, team = T._box_index(box)
    st = _load_best()
    nights, n, w, u = [], 0, 0, 0.0
    for d, s in sorted(st.items(), reverse=True):
        legs, res = [], []
        for l in s["legs"]:
            g, x, _ = T.grade_leg(l, d, ply, team)
            legs.append({**l, "res": g, "actual": x})
            res.append(g)
        status = "open" if any(r is None for r in res) else "graded"
        hit = status == "graded" and "miss" not in res and "hit" in res
        nights.append({"date": d, **s, "legs": legs, "status": status, "hit": hit})
        if status == "graded" and s.get("locked"):
            n += 1
            w += hit
            u += (s["pays_k"] - 1) if hit else -1
    tag = {"ok": {"n": 0, "w": 0, "p": 0.0, "np": 0}, "no": {"n": 0, "w": 0, "p": 0.0, "np": 0}}
    for pk in T._load(T.PICKS, {}).get("picks", []):
        if pk["tipster"] != "sam":
            continue
        rows = T._pred_rows(pk["date"])
        for leg in T.grade_pick(pk, ply, team).get("legs") or []:
            if leg.get("res") not in ("hit", "miss"):
                continue
            c = sam_check(leg, rows.get((T._norm(leg["player"]), leg["stat"])))
            if not c:
                continue
            r = tag["ok" if c["ok"] else "no"]
            r["n"] += 1
            r["w"] += leg["res"] == "hit"
            if c["p"]:
                r["p"] += c["ask"]
                r["np"] += 1
    for r in tag.values():
        r["price"] = round(100 * r["p"] / r["np"]) if r["np"] else None
    return {"tonight": next((x for x in nights if x["date"] == today), None), "rec": {"n": n, "w": w, "u": round(u, 2)},
            "tag": tag, "cush": SAM_CUSH}
