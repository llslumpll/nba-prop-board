"""Site ladders vs Next Level Bets (update 106, Nov 26 2026): the site builds a Next Level-style assists ladder every
night and races his real ladders, the way "Site ladders vs Sam" races Sam.

How he picks (two-season review, claude/nextlevel-review.md): one player whose role grew (teammate out, more minutes,
running the offense), an assists ladder about 2 apart, staked about 1U base / 0.25U next / 0.1U top.
The user's targets: base rung 70%+ and the ladder cashing more often than not. The site's version (safer than his):
  - player: assists over his last 5 at or above his season average, minutes not down, 28+ minutes a game lately,
    7+ games, not out/doubtful/questionable or MINUTES DOWN, game not started; biggest assist jump first;
  - base rung: the safest Kalshi assists rung priced 70-90%;
  - ladder = base + the next rung (+2 assists), plus a +4 bonus rung; stakes 1U / 0.25U / 0.1U.
Replay Oct 28-Nov 25 (23 nights at Kalshi asks): base 21/23 (91%), ladder (base + next) cashed 15/23 (65%),
+4 bonus 6/23 (26%); +8.1U on 31.1U staked. Both halves held (base 88% / 93%). Picked from a grid of 48 settings
on 23 nights, so the live record decides. Locks when its game tips; graded each morning.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import tipsters as T

PATH = Path(__file__).resolve().parent.parent / "data" / "tipsters" / "nlbladder.json"
STEPS = ((0, 1.0), (2, 0.25), (4, 0.1))
PER_NIGHT = 1
ASK_LO, ASK_HI = 0.70, 0.90
MIN_RECENT = 28.0


def _load() -> dict:
    try:
        return json.loads(PATH.read_text())
    except Exception:  # noqa: BLE001
        return {}


def _tip(l: dict) -> datetime | None:
    try:
        return datetime.fromisoformat(str(l.get("tip")).replace("Z", "+00:00"))
    except ValueError:
        return None


def _kladder(date: str, player: str) -> dict:
    try:
        rows = json.loads((T.PRED_DIR / f"{date}.json").read_text())
    except Exception:  # noqa: BLE001
        return {}
    for r in rows.values():
        if r.get("stat") == "ast" and T._norm(r.get("player", "")) == T._norm(player) and r.get("kladder"):
            return {int(k): a for k, _b, a in r["kladder"] if a}
    return {}


def sync(box, sl: dict, now: datetime | None = None, inj=None) -> None:
    """Build tonight's ladder until its game tips."""
    import triple
    import rolespot
    from datetime import date as _date
    now = now or datetime.now(timezone.utc)
    date = (sl or {}).get("date")
    if not date:
        return
    st = _load()
    cur = st.get(date) or {"ladders": []}
    locked = [lad for lad in cur["ladders"] if (_tip(lad) or now) <= now]
    if len(locked) >= PER_NIGHT:
        return
    used = {lad["player"] for lad in locked}
    form = rolespot._form(box, _date.fromisoformat(date))
    best = {}
    for c in triple.candidates(date, box, now, inj):
        if c["stat"] != "ast" or not ASK_LO <= c["ask"] <= ASK_HI or c["player"] in used:
            continue
        f = form.get(T._norm(c["player"]))
        if not f:
            continue
        sea, l5, smin, m5, _n = f
        if not (sea > 0 and l5 >= sea and m5 >= smin and m5 >= MIN_RECENT):
            continue
        b = best.get(c["player"])
        if b is None or c["ask"] > b["ask"]:          # safest rung in the band
            best[c["player"]] = {**c, "sea": round(sea, 1), "l5": round(l5, 1), "smin": round(smin, 1),
                                 "m5": round(m5, 1), "jump": round(l5 / sea, 2)}
    add = []
    for l in sorted(best.values(), key=lambda l: -l["jump"]):
        if len(locked) + len(add) >= PER_NIGHT:
            break
        kl = _kladder(date, l["player"])
        tiers = []
        for step, (off, stake) in enumerate(STEPS):
            rung = l["rung"] + off
            ask = l["ask"] if off == 0 else kl.get(rung)
            if not ask:
                continue
            tiers.append({"player": l["player"], "stat": "ast", "rung": rung, "line": rung - 0.5, "side": "over",
                          "ask": round(ask, 2), "stake": stake, "step": step, "team": l.get("team"), "opp": l.get("opp"),
                          "game_id": l.get("game_id"), "tip": l.get("tip")})
        add.append({"player": l["player"], "team": l.get("team"), "opp": l.get("opp"), "tip": l.get("tip"),
                    "game_id": l.get("game_id"), "l5": l["l5"], "sea": l["sea"], "m5": l["m5"], "smin": l["smin"],
                    "tiers": tiers})
    ladders = locked + add
    if ladders:
        st[date] = {"ladders": ladders, "built": now.strftime("%Y-%m-%d %H:%M UTC")}
    else:
        st.pop(date, None)
    PATH.parent.mkdir(parents=True, exist_ok=True)
    PATH.write_text(json.dumps(st, indent=1, sort_keys=True))


def _pay_ask(a: float) -> float:
    return (1 - a) / a


def _pay_am(o: int) -> float:
    return o / 100 if o > 0 else 100 / -o


def _blank() -> dict:
    return {"n": 0, "w": 0, "price": []}


def _fin(r: dict) -> dict:
    p = r.pop("price")
    r["price"] = round(100 * sum(p) / len(p)) if p else None
    return r


def summary(box, today: str | None) -> dict:
    ply, team = T._box_index(box)
    nights = []
    steps = [_blank() for _ in STEPS]
    u = staked = 0.0
    full = n_lad = 0
    for d, s in sorted(_load().items(), reverse=True):
        lads = []
        for lad in s["ladders"]:
            tiers = []
            for t in lad["tiers"]:
                g, x, _rr = T.grade_leg(t, d, ply, team)
                if g is None and today and d < today:
                    g = "void"
                tiers.append({**t, "res": g, "actual": x})
            lads.append({**lad, "tiers": tiers})
            g_ = [t for t in tiers if t["res"] in ("hit", "miss")]
            if not g_ or len(g_) < len(tiers):
                continue
            n_lad += 1
            two = [t for t in tiers if t.get("step", 0) in (0, 1)]
            full += len(two) == 2 and all(t["res"] == "hit" for t in two)
            for i, t in enumerate(tiers):
                k = min(t.get("step", i), len(steps) - 1)
                steps[k]["n"] += 1
                steps[k]["w"] += t["res"] == "hit"
                steps[k]["price"].append(t["ask"])
                staked += t["stake"]
                u += t["stake"] * _pay_ask(t["ask"]) if t["res"] == "hit" else -t["stake"]
        nights.append({"date": d, "ladders": lads,
                       "status": "open" if any(t["res"] is None for lad in lads for t in lad["tiers"]) else "graded"})
    site = {"ladders": n_lad, "full": full, "u": round(u, 2), "staked": round(staked, 2), "steps": [_fin(x) for x in steps]}

    # his real ladders: picks.json rows for "nlb" that list their higher rungs
    his_steps = [_blank() for _ in STEPS]
    hu = hst = 0.0
    hfull = hn = 0
    recent = []
    try:
        picks = json.loads((T.DIR / "picks.json").read_text())["picks"]
    except Exception:  # noqa: BLE001
        picks = []
    for p in picks:
        if p.get("tipster") != "nlb" or not p.get("rungs") or not p.get("legs"):
            continue
        leg = p["legs"][0]
        gx = [T.grade_leg(lg, p["date"], ply, team)[:2] for lg in p["legs"]]   # two-player ladders: every leg must hit
        if any(g not in ("hit", "miss") for g, _x in gx):
            continue
        xs = [x for _g, x in gx]
        x = xs[0]
        base_rung = int(leg["line"] + 0.5)
        rows = [{"rung": base_rung, "odds": p.get("odds"), "stake": 1.0, "hit": all(g == "hit" for g, _x in gx)}]
        for r in p["rungs"]:
            rows.append({"rung": int(r["r"]), "odds": r.get("odds"), "stake": r.get("u") or 0.1,
                         "hit": all(v is not None and v >= int(r["r"]) for v in xs)})
        hn += 1
        hfull += len(rows) >= 2 and rows[0]["hit"] and rows[1]["hit"]
        for i, r in enumerate(rows):
            k = min(i, len(his_steps) - 1)
            his_steps[k]["n"] += 1
            his_steps[k]["w"] += r["hit"]
            if r["odds"]:
                his_steps[k]["price"].append(1 / (1 + _pay_am(int(r["odds"]))))
                hst += r["stake"]
                hu += r["stake"] * _pay_am(int(r["odds"])) if r["hit"] else -r["stake"]
        recent.append({"date": p["date"], "player": " + ".join(lg["player"] for lg in p["legs"]), "stat": leg["stat"], "actual": x,
                       "rungs": [{"rung": r["rung"], "hit": r["hit"]} for r in rows], "why": p.get("why")})
    his = {"ladders": hn, "full": hfull, "u": round(hu, 2), "staked": round(hst, 2),
           "steps": [_fin(x) for x in his_steps], "recent": sorted(recent, key=lambda r: r["date"], reverse=True)[:8]}
    return {"nights": nights, "tonight": next((x for x in nights if x["date"] == today), None),
            "last": next((x for x in nights if today and x["date"] < today), None), "site": site, "his": his}
