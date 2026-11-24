"""Proven picks (update 99, Nov 21 2026): the site learns which KINDS of picks beat Kalshi's price.

Every night, each kind of pick the site tracks is graded against the price you would have paid (Kalshi ask, your
first-seen tail price, or the posted odds' implied chance). Learning uses wins AND losses: a kind only counts as
beating the price if its hit rate is above what the price implied, over enough picks.

Status (recomputed every build, so kinds move up and down on their own):
  PROVEN     50+ graded and the edge (hit rate minus price) is above zero even at the low end of its 80% range
  PROMISING  15+ graded and hitting at least 2 points above price
  AVOID      30+ graded and hitting 2+ points below price
  TRACKING   everything else (too few picks or about even)
"Proven picks tonight" lists tonight's legs from PROVEN and PROMISING kinds only. Tracking only: no stakes.

Nov 24 (update 101): the site's own slips are graded here too, as one pick each at the price they paid (1 / payout):
Best single (12-11 on the season, 9-1 last 10), Good price 2-leg (8-14, last 3 won), Good price 3-leg (2-12) and the
3x slip. A slip earns a place in "Proven picks tonight" the same way a kind of leg does.
"""
from __future__ import annotations

import math

PROVEN_N, PROMISING_N, AVOID_N = 50, 15, 30
Z = 1.28                       # 80% range
PROMISING_EDGE = 0.02
# rules first set on this date (from a look back over the season): results from here on are the honest test
SINCE = {"strong": "2026-11-20", "agree": "2026-11-20", "disagree": "2026-11-20", "sam_cush": "2026-11-20",
         "sam_nocush": "2026-11-20", "cushion": "2026-11-20", "single": "2026-11-24", "value2": "2026-11-24",
         "value3": "2026-11-24", "triple": "2026-11-24"}
KINDS = {
    "sam_cush": ("Sam + model legs", "Sam's ladder legs where our projection clears his rung by a cushion (points +2, others +1)"),
    "strong": ("Tipster strong spots", "A profitable tipster's prop where our model agrees, he projects 32+ minutes, price better than -125"),
    "cushion": ("Cushion legs", "Tracked cappers' props moved to the safer Kalshi rung (75-92%)"),
    "fav75": ("Favorites 75%+", "Moneyline favorites the market prices at 75%+"),
    "agree": ("Tipster + model agree", "A profitable tipster's prop where our model agrees (not a strong spot)"),
    "safe": ("Safe Lines at your price", "@PrizePicksTacos' safe lines, at the first Kalshi/Polymarket/FanDuel price the site saw"),
    "ladder": ("Site ladder legs", "The site's own Sam-style ladder legs (step 1)"),
    "sam_nocush": ("Sam legs, no cushion", "Sam's legs without the model cushion"),
    "disagree": ("Tipster, model disagrees", "A profitable tipster's prop where our model is on the other side"),
    "single": ("Best single", "The Parlays page's Best single: one pick a night, graded at what it paid"),
    "value2": ("Good price 2-leg", "The Parlays page's Good price 2-leg slip, graded as one pick at what it paid"),
    "value3": ("Good price 3-leg", "The Parlays page's Good price 3-leg slip, graded as one pick at what it paid"),
    "triple": ("3x slip", "The bankroll-builder 3x slip on Your board, graded as one pick at its estimated Kalshi quote"),
}
SLIPS = {("single", 1): "single", ("value", 2): "value2", ("value", 3): "value3"}


def _imp(odds) -> float | None:
    try:
        o = float(odds)
    except (TypeError, ValueError):
        return None
    return 100 / (o + 100) if o > 0 else -o / (-o + 100)


def _stat(rows: list[dict]) -> dict:
    n = len(rows)
    if not n:
        return {"n": 0, "w": 0, "l": 0, "hit": None, "price": None, "edge": None, "u": 0.0, "status": "TRACKING"}
    w = sum(r["hit"] for r in rows)
    p = sum(r["p"] for r in rows) / n
    hit = w / n
    edge = hit - p
    se = math.sqrt(max(p * (1 - p), 1e-6) / n)
    u = sum(((1 - r["p"]) / r["p"]) if r["hit"] else -1 for r in rows)
    if n >= PROVEN_N and edge - Z * se > 0:
        status = "PROVEN"
    elif n >= PROMISING_N and edge >= PROMISING_EDGE:
        status = "PROMISING"
    elif n >= AVOID_N and edge <= -PROMISING_EDGE:
        status = "AVOID"
    else:
        status = "TRACKING"
    return {"n": n, "w": w, "l": n - w, "hit": round(100 * hit, 1), "price": round(100 * p, 1),
            "edge": round(100 * edge, 1), "low": round(100 * (edge - Z * se), 1), "u": round(u, 2),
            "per": round(100 * u / n), "status": status}


def build(box, tp: dict | None, sf: dict | None, fv: dict | None) -> dict:
    import siteladder as SL
    import tipsters as T
    hist: dict[str, list] = {k: [] for k in KINDS}
    tonight: list[dict] = []
    today = (tp or {}).get("today")

    # tipster single props by model tag (graded + tonight)
    for day in (tp or {}).get("days") or []:
        for r in day["rows"]:
            m = r.get("model") or {}
            if r.get("tipster") not in T.AGREE_SRC or m.get("tag") not in ("strong", "agree", "disagree"):
                continue
            if r.get("res") not in ("hit", "miss"):
                continue
            p = ((r.get("your") or {}).get("p")) or _imp(r.get("odds"))
            if p:
                hist[m["tag"]].append({"date": r["date"], "hit": r["res"] == "hit", "p": p})
    for r in (tp or {}).get("tonight") or []:
        m = r.get("model") or {}
        if r.get("tipster") in T.AGREE_SRC and m.get("tag") in ("strong", "agree") and r.get("status") != "graded":
            l = (r.get("legs") or [{}])[0]
            yp = r.get("your") or {}
            tonight.append({"kind": m["tag"], "what": f"{l.get('player')} {'over' if l.get('side', 'over') == 'over' else 'under'} {l.get('line')} {T.LAB.get(l.get('stat'), l.get('stat'))}",
                            "p": yp.get("p") or _imp(r.get("odds")), "price_src": yp.get("src") or "posted odds",
                            "odds": yp.get("odds") or r.get("odds"), "src": r.get("who"), "game": l.get("game_id"),
                            "note": f"our proj {m.get('proj')}" + (f", {m.get('pmin')} min" if m.get("pmin") else "")})

    # Sam legs: cushion / no cushion (Kalshi ask at his rung)
    ply, team = T._box_index(box)
    for pk in T._load(T.PICKS, {}).get("picks", []):
        if pk["tipster"] != "sam":
            continue
        rows = T._pred_rows(pk["date"])
        g = T.grade_pick(pk, ply, team)
        for leg, gl in zip(pk.get("legs") or [], g.get("legs") or pk.get("legs") or []):
            c = SL.sam_check(leg, rows.get((T._norm(leg["player"]), leg["stat"])))
            if not c or not c.get("ask"):
                continue
            k = "sam_cush" if c["ok"] else "sam_nocush"
            if gl.get("res") in ("hit", "miss"):
                hist[k].append({"date": pk["date"], "hit": gl["res"] == "hit", "p": c["ask"]})
            elif pk["date"] == today and c["ok"]:
                tonight.append({"kind": k, "what": f"{leg['player']} {c['rung']}+ {T.LAB.get(leg['stat'], leg['stat'])}",
                                "p": c["ask"], "price_src": "Kalshi", "odds": T.p_to_am(c["ask"]), "src": "Sam",
                                "game": c.get("game_id"), "note": f"our proj {c['proj']}"})

    # cushion legs (the combo pools: every cushion leg the site built, one per player)
    for c in (tp or {}).get("combos") or []:
        if c.get("v") not in (2, 3):
            continue
        for l in c.get("pool") or []:
            p = l.get("ask") or l.get("p")
            if not p:
                continue
            if l.get("res") in ("hit", "miss"):
                hist["cushion"].append({"date": c["date"], "hit": l["res"] == "hit", "p": p})
            elif c["date"] == today and l.get("stat") != "ml":
                tonight.append({"kind": "cushion", "what": f"{l['player']} {'' if l.get('side', 'over') == 'over' else 'NO '}{l['rung']}+ {T.LAB.get(l['stat'], l['stat'])}",
                                "p": p, "price_src": "Kalshi", "odds": T.p_to_am(p),
                                "src": l.get("tipster"), "game": l.get("game_id"),
                                "note": f"their line {l.get('their_line')}" if l.get("their_line") else ""})

    # favorites 75%+
    for d in (fv or {}).get("days") or []:
        for r in d["rows"]:
            p = r.get("cost") or r.get("p")
            if p and r.get("won") is not None:
                hist["fav75"].append({"date": r["date"], "hit": bool(r["won"]), "p": p})
    for r in (fv or {}).get("tonight") or []:
        b = r.get("best") or {}
        tonight.append({"kind": "fav75", "what": f"{r['fav']} to win", "p": b.get("p") or r["p"],
                        "price_src": b.get("book") or "fair", "odds": b.get("odds") or r.get("fair"), "src": None,
                        "game": r.get("game_id"), "note": f"{r['away']} @ {r['home']}"})

    # safe lines at your price
    for d in (sf or {}).get("days") or []:
        for r in d.get("rows") or []:
            yp = (r.get("your") or {}).get("p")
            if yp and r.get("res") in ("hit", "miss"):
                hist["safe"].append({"date": d["date"], "hit": r["res"] == "hit", "p": yp})
    for r in (sf or {}).get("tonight") or []:
        if r.get("tag") == "good" and r.get("cost"):
            tonight.append({"kind": "safe", "what": f"{r['player']} over {r['line']} {T.LAB.get(r['stat'], r['stat'])}",
                            "p": r["cost"], "price_src": (r.get("your") or {}).get("src") or "your price",
                            "odds": r.get("your_odds"), "src": "Safe Lines", "game": None, "note": ""})

    # site ladder legs, step 1
    sx = (tp or {}).get("siteladder") or {}
    for n in sx.get("nights") or []:
        for lad in n.get("ladders") or []:
            for l in (lad.get("tiers") or [{}])[0].get("legs") or []:
                p = l.get("ask") or l.get("p")
                if p and l.get("res") in ("hit", "miss"):
                    hist["ladder"].append({"date": n["date"], "hit": l["res"] == "hit", "p": p})

    # the site's own slips, one pick each at the price they paid (update 101)
    import glob
    import json
    from pathlib import Path
    pdir = Path(__file__).resolve().parent.parent / "data" / "parlays"
    for f in sorted(glob.glob(str(pdir / "*.json"))):
        try:
            day = json.loads(Path(f).read_text())
        except Exception:  # noqa: BLE001
            continue
        for v in day.values():
            k = SLIPS.get((v.get("family"), v.get("size")))
            pays = v.get("pays_final") or v.get("pays")
            if not k or not pays or pays <= 1:
                continue
            if v.get("status") == "graded" and v.get("hit") is not None:
                hist[k].append({"date": v["date"], "hit": bool(v["hit"]), "p": 1 / pays})
            elif v.get("date") == today and v.get("status") != "graded":
                legs = v.get("legs") or []
                tonight.append({"kind": k, "what": " + ".join(l.get("label") or l.get("kalshi_bet") or "?" for l in legs),
                                "p": 1 / pays, "price_src": "Kalshi" if v.get("all_kalshi") else "best price",
                                "odds": T.p_to_am(1 / pays), "src": KINDS[k][0],
                                "game": legs[0].get("game_id") if len(legs) == 1 else None,
                                "note": f"pays {pays:.2f}x" if len(legs) > 1 else ""})
    try:
        import triple
        for d, t in triple._load().items():
            if not t.get("locked") and d != today:
                continue
            res = [T.grade_leg(l, d, ply, team)[0] for l in t["legs"]]
            if today and d < today:   # finished night: no result = didn't play = void
                res = [r or "void" for r in res]
            if any(r is None for r in res):
                if d == today:
                    tonight.append({"kind": "triple", "what": " + ".join(f"{l['player']} {l['rung']}+ {T.LAB.get(l['stat'], l['stat'])}" for l in t["legs"]),
                                    "p": 1 / t["quote_est"], "price_src": "Kalshi", "odds": T.p_to_am(1 / t["quote_est"]),
                                    "src": "3x slip", "game": None, "note": f"about {t['quote_est']:.1f}x"})
                continue
            if t.get("locked"):
                hist["triple"].append({"date": d, "hit": "miss" not in res and "hit" in res, "p": 1 / t["quote_est"]})
    except Exception as e:  # noqa: BLE001
        print(f"Proven picks: 3x slip history failed: {e}")

    kinds = []
    for k, (name, what) in KINDS.items():
        s = _stat(hist[k])
        fw = None
        if k in SINCE:
            f = _stat([r for r in hist[k] if r["date"] >= SINCE[k]])
            fw = {"since": SINCE[k], "w": f["w"], "l": f["l"], "u": f["u"]}
            # the forward test can demote: 10+ picks since the rule was set and losing to the price -> TRACKING
            if f["n"] >= 10 and (f["edge"] or 0) < 0 and s["status"] in ("PROVEN", "PROMISING"):
                s = {**s, "status": "TRACKING", "demoted": True}
        kinds.append({"key": k, "name": name, "what": what, **s, "fwd": fw})
    order = {"PROVEN": 0, "PROMISING": 1, "TRACKING": 2, "AVOID": 3}
    kinds.sort(key=lambda x: (order[x["status"]], -(x["edge"] or -99)))
    status = {k["key"]: k["status"] for k in kinds}

    # tonight: only legs from PROVEN / PROMISING kinds, one per player-prop, best kind first
    seen, picks = set(), []
    for t in sorted(tonight, key=lambda t: (order[status[t["kind"]]], -(t["p"] or 0))):
        if status[t["kind"]] not in ("PROVEN", "PROMISING") or not t.get("p"):
            continue
        key = t["what"].lower()
        if key in seen:
            continue
        seen.add(key)
        picks.append({**t, "status": status[t["kind"]], "kind_name": KINDS[t["kind"]][0]})
    # a short combo: the two highest-chance picks from different games (short combos keep Kalshi's combo cut small)
    pair, games = [], set()
    for t in sorted(picks, key=lambda t: -t["p"]):
        g = t.get("game")
        if g and g in games:
            continue
        pair.append(t)
        if g:
            games.add(g)
        if len(pair) == 2:
            break
    combo = None
    if len(pair) == 2:
        pay = 1 / (pair[0]["p"] * pair[1]["p"])
        combo = {"legs": pair, "pays": round(pay, 2)}
    return {"kinds": kinds, "tonight": picks, "combo": combo,
            "rules": {"proven_n": PROVEN_N, "promising_n": PROMISING_N, "avoid_n": AVOID_N}}
