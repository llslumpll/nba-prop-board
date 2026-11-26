"""Best 5 on Kalshi (update 102, Nov 24 2026): the five Kalshi legs a night with the biggest REAL edge.

Why: the model's Best 5 (five highest-chance picks per stat) went 473-345 but lost 52U: high-chance picks are mostly
expensive, and no re-ranking of the model's own picks beat the price (every ranking tried lost 6-9% a pick when each
night was judged only on the other nights). Kalshi rung legs judged by how legs like them really finished do:
replay Oct 28-Nov 22, each night graded with only the other nights' history:
    top 5 a night, any price 50-97%: 87-23, +9.1U (+8.3%/bet)
    top 5 a night, priced 60-90%:    97-13, +17.1U (+15.6%/bet)   <- used (60-90% was chosen after seeing both)
Both were in profit in each half-month. Legs come from triple.candidates (same filters as the 3x slip: game not started,
player not out/doubtful/questionable, no MINUTES DOWN flag, 5+ games) and each leg's real chance from triple.calibrate.
One leg per player, edge over 1 point. A leg locks when its game tips; open slots refill until then.
Graded every morning at 1U per leg at the Kalshi ask.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import tipsters as T
import triple

PATH = Path(__file__).resolve().parent.parent / "data" / "tipsters" / "bestk.json"
SIZE = 5
ASK_LO, ASK_HI = 0.60, 0.90
MIN_EDGE = 0.01


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


def sync(box, sl: dict, now: datetime | None = None, inj=None) -> None:
    now = now or datetime.now(timezone.utc)
    date = (sl or {}).get("date")
    if not date:
        return
    st = _load()
    cur = st.get(date) or {"legs": []}
    locked = [l for l in cur["legs"] if (_tip(l) or now) <= now]
    players = {l["player"] for l in locked}
    open_n = max(SIZE - len(locked), 0)
    pool = [c for c in triple.candidates(date, box, now, inj) if ASK_LO <= c["ask"] <= ASK_HI]
    for c in pool:
        c["edge"] = round(c["est"] - c["ask"], 4)
    add = []
    for c in sorted(pool, key=lambda c: -c["edge"]):
        if len(add) >= open_n:
            break
        if c["edge"] <= MIN_EDGE or c["player"] in players:
            continue
        add.append(c)
        players.add(c["player"])
    legs = sorted(locked + add, key=lambda l: (l.get("tip") or "", -l.get("edge", 0)))
    if legs:
        st[date] = {"legs": legs, "built": now.strftime("%Y-%m-%d %H:%M UTC")}
    else:
        st.pop(date, None)
    PATH.parent.mkdir(parents=True, exist_ok=True)
    PATH.write_text(json.dumps(st, indent=1, sort_keys=True))


def summary(box, today: str | None, load=None) -> dict:
    ply, team = T._box_index(box)
    nights, n, w, u = [], 0, 0, 0.0
    last7 = {"n": 0, "w": 0, "u": 0.0}
    dates = sorted((load or _load)().items(), reverse=True)
    recent = [d for d, _ in dates if today and d < today][:7]
    for d, s in dates:
        legs = []
        for l in s["legs"]:
            g, x, rr = T.grade_leg(l, d, ply, team)
            if g is None and today and d < today:
                g = "void"
            legs.append({**l, "res": g, "actual": x, "cut": T.cut_short(d, l, rr) if g in ("hit", "miss") else None})
        g_ = [l for l in legs if l["res"] in ("hit", "miss")]
        nights.append({"date": d, "legs": legs, "w": sum(l["res"] == "hit" for l in g_), "l": sum(l["res"] == "miss" for l in g_),
                       "u": round(sum(((1 - l["ask"]) / l["ask"]) if l["res"] == "hit" else -1.0 for l in g_), 2),
                       "status": "open" if any(l["res"] is None for l in legs) else "graded"})
        for l in legs:
            if l["res"] not in ("hit", "miss"):
                continue
            x = ((1 - l["ask"]) / l["ask"]) if l["res"] == "hit" else -1.0
            n += 1
            w += l["res"] == "hit"
            u += x
            if d in recent:
                last7["n"] += 1
                last7["w"] += l["res"] == "hit"
                last7["u"] += x
    last7["u"] = round(last7["u"], 2)
    return {"nights": nights, "tonight": next((x for x in nights if x["date"] == today), None),
            "last": next((x for x in nights if today and x["date"] < today), None),
            "rec": {"n": n, "w": w, "u": round(u, 2)}, "last7": last7}
